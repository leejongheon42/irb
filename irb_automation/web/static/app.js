// IRB 자동화 솔루션 - 최소한의 클라이언트 스크립트 (CSP: script-src 'self')
(function () {
  "use strict";

  // 탭: <div class="tabs"><a href="#x" data-tab="x"> + <section class="tab-panel" id="tab-x">
  function activateTab(name) {
    var links = document.querySelectorAll(".tabs a[data-tab]");
    if (!links.length) return;
    var found = false;
    links.forEach(function (a) {
      var on = a.getAttribute("data-tab") === name;
      a.classList.toggle("active", on);
      if (on) found = true;
    });
    if (!found) name = links[0].getAttribute("data-tab");
    links.forEach(function (a) { a.classList.toggle("active", a.getAttribute("data-tab") === name); });
    document.querySelectorAll(".tab-panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "tab-" + name);
    });
  }
  document.querySelectorAll(".tabs a[data-tab]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var name = a.getAttribute("data-tab");
      history.replaceState(null, "", "#" + name);
      activateTab(name);
    });
  });
  activateTab((location.hash || "").replace("#", ""));
  window.addEventListener("hashchange", function () { activateTab((location.hash || "").replace("#", "")); });

  // 조건부 표시: data-show-when="field=value" 또는 "field" (체크박스 체크 시)
  function evaluateConditions() {
    document.querySelectorAll("[data-show-when]").forEach(function (el) {
      var cond = el.getAttribute("data-show-when");
      var parts = cond.split("=");
      var name = parts[0];
      var inputs = document.querySelectorAll('[name="' + name + '"]');
      var show = false;
      inputs.forEach(function (inp) {
        if (inp.type === "checkbox" || inp.type === "radio") {
          if (inp.checked && (parts.length === 1 || inp.value === parts[1])) show = true;
        } else if (parts.length === 1 ? !!inp.value : inp.value === parts[1]) {
          show = true;
        }
      });
      el.classList.toggle("hidden", !show);
    });
  }
  document.addEventListener("change", evaluateConditions);
  evaluateConditions();

  // 확인 대화상자: <form data-confirm="메시지">
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // 가명처리 기법별 파라미터 표시
  function syncParams(select) {
    var row = select.closest("tr");
    if (!row) return;
    row.querySelectorAll(".param").forEach(function (p) {
      var actions = (p.getAttribute("data-actions") || "").split(" ");
      p.classList.toggle("show", actions.indexOf(select.value) >= 0);
    });
  }
  document.querySelectorAll("select.action-select").forEach(function (s) {
    syncParams(s);
    s.addEventListener("change", function () { syncParams(s); });
  });

  // 인쇄 / 뒤로가기
  document.querySelectorAll("[data-print]").forEach(function (b) {
    b.addEventListener("click", function () { window.print(); });
  });
  document.querySelectorAll("[data-back]").forEach(function (b) {
    b.addEventListener("click", function () { history.back(); });
  });
})();

// 화면별 사용 가이드: 처음 방문한 화면에서 자동으로 열고, 닫으면 다시 자동으로 열지 않는다(브라우저에만 기억).
(function () {
  "use strict";
  var g = document.getElementById("page-guide");
  if (!g) return;
  var key = "irb-guide-seen:" + g.getAttribute("data-guide-key");
  function get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* 저장 불가 환경 */ } }
  var auto = get("irb-guide-auto") !== "off";
  var box = g.querySelector("[data-guide-auto]");
  if (box) {
    box.checked = auto;
    box.addEventListener("change", function () { set("irb-guide-auto", box.checked ? "on" : "off"); });
  }
  if (auto && !get(key)) g.open = true;
  g.addEventListener("toggle", function () { if (!g.open) set(key, "1"); });
  var close = g.querySelector("[data-guide-close]");
  if (close) close.addEventListener("click", function () { g.open = false; set(key, "1"); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && g.open) { g.open = false; set(key, "1"); } });
})();
