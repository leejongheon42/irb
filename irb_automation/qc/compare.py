"""Compare only like-for-like facts, without choosing a reference document."""
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from re import match
from typing import TYPE_CHECKING, NotRequired, TypedDict

from .facts import DOSE_AMOUNT, Fact
from .rules import rules

if TYPE_CHECKING:
    from .checks import QCDoc


class Location(TypedDict):
    doc: str
    name: str
    quote: str
    value: str
    norm: str
    unique: bool
    amount_unique: NotRequired[bool]


class Comparison(TypedDict):
    key: str
    field: str
    scope: str
    locations: list[Location]
    uncertain: bool
    different: bool
    reference_blocked: bool


def comparisons(docs: Iterable["QCDoc"]) -> Iterable[Comparison]:
    groups: dict[tuple[str, str, str], list[tuple["QCDoc", Fact]]] = defaultdict(list)
    documents = list(docs)
    order = {doc.key: i for i, doc in enumerate(documents)}
    for doc in documents:
        for fact in doc.facts:
            groups[fact.key, fact.field or fact.key, fact.scope].append((doc, fact))
    for key, field, scope in tuple(groups):
        if key != "dosage" or not scope.endswith("|시험약"):
            continue
        generic = scope.removesuffix("|시험약")
        peers = [candidate for kind, name, candidate in groups if kind == key and name == field
                 and candidate.startswith(generic + "|")]
        if len(peers) == 1 and (key, field, generic) in groups:
            groups[key, field, scope].extend(groups.pop((key, field, generic)))
    batches = [(key, field, scope, entries, False) for (key, field, scope), entries in sorted(groups.items())
               if len({d.key for d, _ in entries}) >= 2]
    for key, compatible in rules().get("compatible_fields", {}).items():
        pools: dict[str, list[tuple["QCDoc", Fact]]] = defaultdict(list)
        for (kind, field, scope), entries in groups.items():
            if kind == key and field in compatible and len({d.key for d, _ in entries}) == 1:
                pools[scope].extend(entries)
        for scope, entries in sorted(pools.items()):
            if len({d.key for d, _ in entries}) >= 2 and len({f.field for _, f in entries}) > 1:
                batches.append((key, key, scope, entries, True))
    for key, field, scope, entries, mixed in batches:
        if len({d.key for d, _ in entries}) < 2:
            continue
        locations: list[Location] = []
        values: dict[str, set[str]] = defaultdict(set)
        units = set()
        uncertain = any(f.ambiguous for _, f in entries)
        for doc, fact in sorted(entries, key=lambda pair: order[pair[0].key]):
            norm = fact.value.split("/", 1)[1] if key == "dosage" else fact.value
            if key in ("dosage", "duration"):
                token = match(r"([\d.]+)(.*)", norm)
                if token:
                    number, unit = token.groups()
                    units.add(unit)
                    if mixed and key == "dosage" and fact.field == "dose_unit":
                        if fact.raw.startswith(("1일", "하루", "매일")):
                            frequency = Decimal(fact.value.split("회/", 1)[0])
                            norm = f"{(Decimal(number) * frequency).normalize():f}{unit}"
                        else:
                            uncertain = True
            values[doc.key].add(norm)
            location = Location(doc=doc.key, name=doc.name, quote=fact.line, value=fact.raw, norm=norm,
                                unique=bool(fact.raw and doc.text.count(fact.raw) == 1))
            if key == "dosage":
                amount = DOSE_AMOUNT.search(fact.raw)
                location["amount_unique"] = bool(amount and doc.text.count(amount[0]) == 1)
            if location not in locations:
                locations.append(location)
        different = len({frozenset(v) for v in values.values()}) > 1
        uncertain |= len(units) > 1 or (mixed and key != "dosage")
        uncertain |= key == "dosage" and different and any(len(v) > 1 for v in values.values())
        yield Comparison(key=key, field=field, scope=scope, locations=locations, different=different,
                         uncertain=uncertain, reference_blocked=mixed or len(units) > 1)
