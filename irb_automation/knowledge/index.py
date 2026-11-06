"""검색 색인: 한국어 대응 토크나이저 + BM25 + 메모리 캐시 + 스니펫·하이라이트.

- 한글: 형태소 분석기 없이 동작하도록 문자 2-gram(1글자 단어는 1-gram)으로 색인한다.
- 영문·숫자: 단어 단위(밑줄로 이어진 코드는 전체와 부분 모두).
- 조문 표기 정규화: '제28조의2', '28조의2', '제 28 조의 2'는 같은 토큰(a28_2)으로,
  '제16조제3항'은 a16 + a16p3 토큰으로 색인해 조문 질의가 정확히 맞도록 한다.
"""
from __future__ import annotations

import math
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup, escape

# ------------------------------------------------------------------ 토크나이저
_ARTICLE = re.compile(
    r"(?:제\s*)?(\d{1,3})\s*조(?:\s*의\s*(\d{1,2}))?"
    r"(?:\s*(?:제\s*)?(\d{1,2})\s*항)?"
    r"(?:\s*(?:제\s*)?(\d{1,2})\s*호)?"
)
_HANGUL_RUN = re.compile(r"[가-힣]+")
_WORD = re.compile(r"[a-z0-9]+(?:[._\-][a-z0-9]+)*")
# 하이라이트용 조사(질의어 끝에서 제거)
_PARTICLES = ("에서는", "으로는", "에게는", "에서", "으로", "에게", "까지", "부터", "은", "는", "이", "가", "을", "를",
              "의", "에", "로", "와", "과", "도", "만")


# 문장 끝 어미·조사 등 의미 없는 한글 2-gram(질의·문서 양쪽에서 제외)
STOP_BIGRAMS = frozenset({
    "니다", "습니", "합니", "입니", "있습", "됩니", "십시", "시오", "하십", "예요", "에요", "까요", "나요", "가요",
    "해요", "어요", "세요", "하는", "에서", "으로", "하고", "이다", "것이", "있는", "하여", "되는", "한다", "된다",
    "있다", "없다", "하다", "뭐예", "무엇", "어떻", "떻게", "인가", "는지", "은지", "할수", "수있",
    "엇을", "해야", "야하", "하나", "려면", "으려", "받으", "되나", "할까", "인데", "뭔가", "는데",
})


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def article_tokens(text: str) -> tuple[list[str], str]:
    """조문 토큰과, 조문 표기를 공백으로 지운 나머지 텍스트를 반환."""
    tokens: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        art, sub, para, item = m.group(1), m.group(2), m.group(3), m.group(4)
        base = f"a{int(art)}" + (f"_{int(sub)}" if sub else "")
        tokens.append(base)
        if para:
            tokens.append(f"{base}p{int(para)}")
            if item:
                tokens.append(f"{base}p{int(para)}i{int(item)}")
        return " "

    rest = _ARTICLE.sub(_sub, text)
    return tokens, rest


def tokenize(text: str) -> list[str]:
    t = normalize(text)
    tokens, rest = article_tokens(t)
    for run in _HANGUL_RUN.findall(rest):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(bg for bg in (run[i : i + 2] for i in range(len(run) - 1)) if bg not in STOP_BIGRAMS)
    for word in _WORD.findall(rest):
        if len(word) < 2 and not word.isdigit():
            continue
        tokens.append(word)
        if "_" in word or "-" in word:
            tokens.extend(p for p in re.split(r"[_\-]", word) if len(p) >= 2)
    return tokens


def query_terms(query: str) -> list[str]:
    """하이라이트에 쓸 질의어(공백 단위, 끝 조사 제거, 2자 이상)."""
    terms: list[str] = []
    for raw in re.split(r"[\s,.;:!?()\[\]{}\"'·/]+", unicodedata.normalize("NFKC", query or "")):
        w = raw.strip()
        if not w:
            continue
        if re.fullmatch(r"[가-힣]+", w) and len(w) > 2:
            for p in _PARTICLES:
                if w.endswith(p) and len(w) - len(p) >= 2:
                    w = w[: -len(p)]
                    break
        if len(w) >= 2:
            terms.append(w)
    # 조문 표기는 원형 그대로도 하이라이트
    for m in _ARTICLE.finditer(unicodedata.normalize("NFKC", query or "")):
        terms.append(m.group(0).strip())
    return sorted(set(terms), key=len, reverse=True)


# ------------------------------------------------------------------ 하이라이트·스니펫
def highlight(text: str, terms: list[str]) -> Markup:
    """텍스트를 escape한 뒤 질의어에만 <mark>를 씌운다(사용자 입력이 HTML로 해석되지 않음)."""
    if not text:
        return Markup("")
    spans: list[tuple[int, int]] = []
    lowered = text.lower()
    for term in terms:
        t = term.lower()
        if not t:
            continue
        start = 0
        while True:
            i = lowered.find(t, start)
            if i < 0:
                break
            spans.append((i, i + len(t)))
            start = i + len(t)
        # 조문 표기는 공백 차이를 허용해 원문에서 찾는다
        if re.search(r"\d\s*조", t):
            for m in _ARTICLE.finditer(text):
                if article_tokens(normalize(m.group(0)))[0][:1] == article_tokens(normalize(t))[0][:1]:
                    spans.append((m.start(), m.end()))
    if not spans:
        return Markup(escape(text))
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = []
    pos = 0
    for s, e in merged:
        out.append(escape(text[pos:s]))
        out.append(Markup("<mark>") + escape(text[s:e]) + Markup("</mark>"))
        pos = e
    out.append(escape(text[pos:]))
    return Markup("").join(out)


_SENTENCE = re.compile(r"(?<=[.!?。])\s+|(?<=다\.)\s*|\n+")


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE.split(text or "") if p and p.strip()]
    return parts or [text.strip()] if text else []


def best_sentences(text: str, q_tokens: set[str], limit_chars: int = 220) -> str:
    """질의 토큰을 가장 많이 포함한 문장(들)로 스니펫을 만든다."""
    sents = sentences(text)
    if not sents:
        return ""
    scored = []
    for i, s in enumerate(sents):
        toks = set(tokenize(s))
        scored.append((len(toks & q_tokens), -i, s))
    scored.sort(reverse=True)
    best = scored[0][2]
    if scored[0][0] == 0:
        best = sents[0]
    if len(best) < limit_chars // 2:
        idx = sents.index(best)
        if idx + 1 < len(sents):
            best = f"{best} {sents[idx + 1]}"
    if len(best) > limit_chars:
        best = best[: limit_chars - 1].rstrip() + "…"
    return best


# ------------------------------------------------------------------ BM25
@dataclass
class IndexedChunk:
    chunk_id: int
    source_id: int
    kind: str
    title: str
    ref: str
    url: str
    heading: str
    text: str
    protocol_id: int | None
    tf: Counter[str] = field(default_factory=Counter)
    length: int = 0


class BM25Index:
    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: list[IndexedChunk] = []
        self.df: Counter[str] = Counter()
        self.avgdl = 0.0

    def add(self, doc: IndexedChunk, tokens: list[str]) -> None:
        doc.tf = Counter(tokens)
        doc.length = max(1, len(tokens))
        self.docs.append(doc)
        self.df.update(doc.tf.keys())

    def finalize(self) -> None:
        self.avgdl = (sum(d.length for d in self.docs) / len(self.docs)) if self.docs else 0.0

    def idf(self, term: str) -> float:
        n = len(self.docs)
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query_tokens: list[str], *, kinds: set[str] | None = None,
               allow=None, limit: int = 20) -> list[tuple[float, IndexedChunk]]:
        if not self.docs or not query_tokens:
            return []
        q = Counter(query_tokens)
        idf = {t: self.idf(t) for t in q if t in self.df}
        if not idf:
            return []
        results = []
        for d in self.docs:
            if kinds and d.kind not in kinds:
                continue
            if allow is not None and not allow(d):
                continue
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * d.length / (self.avgdl or 1))
            for t, w in idf.items():
                tf = d.tf.get(t)
                if tf:
                    score += w * (tf * (self.k1 + 1)) / (tf + norm) * (1 + 0.1 * (q[t] - 1))
            if score > 0:
                results.append((score, d))
        results.sort(key=lambda x: (-x[0], x[1].chunk_id))
        return results[:limit]


def chunk_tokens(title: str, ref: str, heading: str, text: str, title_boost: int = 2) -> list[str]:
    head = tokenize(" ".join(x for x in (title, ref, heading) if x))
    return head * max(1, title_boost) + tokenize(text)


# ------------------------------------------------------------------ 캐시
_lock = threading.Lock()
_cache: dict[str, tuple[Any, BM25Index]] = {}


def cached_index(engine_key: str, stamp: Any, build) -> BM25Index:
    """엔진(DB)별로 색인을 캐시하고, 지식 데이터의 변경 표식(stamp)이 바뀌면 다시 만든다."""
    with _lock:
        hit = _cache.get(engine_key)
        if hit and hit[0] == stamp:
            return hit[1]
    index = build()
    with _lock:
        _cache[engine_key] = (stamp, index)
    return index


def invalidate(engine_key: str | None = None) -> None:
    with _lock:
        if engine_key is None:
            _cache.clear()
        else:
            _cache.pop(engine_key, None)
