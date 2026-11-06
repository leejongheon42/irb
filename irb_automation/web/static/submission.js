// 웹 문서 제출: 드래그&드롭 다중 업로드, 파일별 서류 종류 추정 (CSP: script-src 'self')
(function () {
  "use strict";
  var form = document.querySelector("form.upload-form");
  if (!form) return;
  var input = form.querySelector(".file-input");
  var zone = form.querySelector(".dropzone");
  var list = form.querySelector(".file-list");
  var tpl = form.querySelector(".doc-type-template");
  var noJs = form.querySelector(".no-js-type");
  var maxFiles = parseInt(form.getAttribute("data-max-files") || "20", 10);
  var allowed = [".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".jpg", ".jpeg", ".png", ".zip"];
  var canAssign = typeof DataTransfer !== "undefined";
  if (noJs) noJs.classList.add("hidden");

  function ext(n) { var i = n.lastIndexOf("."); return i >= 0 ? n.slice(i).toLowerCase() : ""; }
  function fmt(s) { return s > 1048576 ? (s / 1048576).toFixed(1) + "MB" : Math.max(1, Math.round(s / 1024)) + "KB"; }
  function setFiles(files) {
    if (!canAssign) return;
    var dt = new DataTransfer();
    for (var i = 0; i < files.length && i < maxFiles; i++) dt.items.add(files[i]);
    input.files = dt.files;
  }
  function hasOption(sel, v) {
    for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === v) return true;
    return false;
  }
  function render() {
    while (list.firstChild) list.removeChild(list.firstChild);
    var files = input.files;
    Array.prototype.forEach.call(files, function (f, i) {
      var row = document.createElement("div");
      row.className = "file-row";
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = f.name + " (" + fmt(f.size) + ")";
      var sel = tpl.cloneNode(true);
      sel.classList.remove("hidden");
      sel.classList.remove("doc-type-template");
      sel.removeAttribute("aria-hidden");
      sel.removeAttribute("tabindex");
      sel.name = "doc_type_" + i;
      sel.setAttribute("aria-label", f.name + " 서류 종류");
      var hint = document.createElement("span");
      hint.className = "file-hint small muted";
      if (allowed.indexOf(ext(f.name)) < 0) {
        row.classList.add("file-bad");
        hint.textContent = "허용되지 않는 형식 - 제출 시 거부됩니다";
      }
      var rm = document.createElement("button");
      rm.type = "button";
      rm.className = "btn-sm";
      rm.textContent = "빼기";
      rm.disabled = !canAssign;
      rm.addEventListener("click", function () {
        var arr = Array.prototype.slice.call(input.files);
        arr.splice(i, 1);
        setFiles(arr);
        render();
      });
      row.appendChild(name); row.appendChild(sel); row.appendChild(hint); row.appendChild(rm);
      list.appendChild(row);
      fetch("/documents/guess?name=" + encodeURIComponent(f.name), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (g) {
          if (!g || row.classList.contains("file-bad")) return;
          if (g.confidence >= 0.5 && hasOption(sel, g.doc_type)) {
            sel.value = g.doc_type;
            hint.textContent = "추정: " + g.label + " (" + g.reason + ")";
          } else {
            hint.textContent = "파일명으로 추정 불가 - 종류를 선택하거나 '자동 추정'(내용 분석)을 그대로 두십시오";
          }
        })
        .catch(function () {});
    });
  }
  input.addEventListener("change", render);
  ["dragenter", "dragover"].forEach(function (t) {
    zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    zone.addEventListener(t, function () { zone.classList.remove("dragover"); });
  });
  zone.addEventListener("drop", function (e) {
    e.preventDefault();
    if (!e.dataTransfer || !e.dataTransfer.files.length || !canAssign) return;
    var arr = Array.prototype.slice.call(input.files).concat(Array.prototype.slice.call(e.dataTransfer.files));
    if (arr.length > maxFiles) { window.alert("한 번에 " + maxFiles + "개까지 제출할 수 있습니다."); arr = arr.slice(0, maxFiles); }
    setFiles(arr);
    render();
  });
  zone.addEventListener("click", function (e) { if (e.target !== input) input.click(); });
  zone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  form.addEventListener("submit", function (e) {
    if (!input.files.length) { e.preventDefault(); window.alert("제출할 파일을 선택하십시오."); }
  });
})();
