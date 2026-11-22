// 문서 자동화 파이프라인: 진행 상태 폴링, 전체 선택
(function () {
  "use strict";
  document.querySelectorAll(".pl-check-all").forEach(function (all) {
    all.addEventListener("change", function () {
      var table = all.closest("table");
      table.querySelectorAll('input[name="fields"]').forEach(function (cb) { cb.checked = all.checked; });
    });
  });

  var box = document.getElementById("pl-progress");
  if (!box || box.dataset.active !== "1") return;
  var runId = box.dataset.runId;
  var labels = { queued: "대기", running: "진행 중", succeeded: "완료", failed: "실패", skipped: "건너뜀" };
  var badge = { succeeded: "b-ok", failed: "b-danger", running: "b-info" };
  var tries = 0;

  function update(data) {
    data.steps.forEach(function (s) {
      var li = box.querySelector('[data-step="' + s.name + '"]');
      if (!li) return;
      li.className = "pl-step pl-" + s.status;
      var b = li.querySelector(".pl-badge .badge");
      if (b) { b.textContent = labels[s.status] || s.status; b.className = "badge " + (badge[s.status] || ""); }
      var m = li.querySelector(".pl-msg");
      if (m) m.textContent = s.message || "";
      var t = li.querySelector(".pl-ms");
      if (t && s.duration_ms) t.textContent = (s.duration_ms / 1000).toFixed(1) + "초";
    });
  }

  function poll() {
    tries += 1;
    fetch("/api/pipeline/" + runId, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (data) {
        update(data);
        if (data.active) { setTimeout(poll, tries < 20 ? 1500 : 4000); }
        else { window.location.reload(); }
      })
      .catch(function () { if (tries < 60) setTimeout(poll, 5000); });
  }
  setTimeout(poll, 1000);
})();
