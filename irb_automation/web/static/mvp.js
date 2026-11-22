(() => {
  const form = document.querySelector('#mvp-upload');
  if (form) {
    const input = document.querySelector('#mvp-files');
    const zone = document.querySelector('#mvp-dropzone');
    const status = document.querySelector('#upload-status');
    const rows = document.querySelector('#mvp-file-rows');
    const rowTemplate = document.querySelector('#mvp-upload-row');
    const start = document.querySelector('#mvp-start button');
    const clear = document.querySelector('#mvp-clear button');
    let uploading = false;

    async function upload(files) {
      if (uploading || !files.length) return;
      const data = new FormData(form);
      data.delete('files');
      for (const file of files) data.append('files', file);
      delete status.dataset.error;
      status.textContent = '서류를 업로드하고 있습니다.';
      uploading = start.disabled = clear.disabled = input.disabled = true;
      try {
        const response = await fetch(form.action, { method: 'POST', body: data, headers: { Accept: 'application/json' } });
        if (!response.ok || response.redirected) {
          status.dataset.error = '';
          status.textContent = '업로드하지 못했습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.';
          return;
        }
        const added = await response.json();
        for (const file of added) {
          const row = rowTemplate.content.firstElementChild.cloneNode(true);
          const fields = {
            name: file.name, fmt: file.fmt.toUpperCase() || '—', size: `${file.size.toLocaleString()} B`,
            doc_type_label: file.doc_type_label || '—', status: file.status === 'ok' ? '업로드됨' : '거부됨',
            message: file.message,
          };
          for (const [key, value] of Object.entries(fields)) row.querySelector(`[data-field="${key}"]`).textContent = value;
          row.querySelector('form').action = `/mvp/files/${file.id}/delete`;
          row.querySelector('button').setAttribute('aria-label', `${file.name} 삭제`);
          row.dataset.fileStatus = file.status;
          rows.append(row);
        }
        const accepted = added.filter(file => file.status === 'ok').length;
        status.textContent = `업로드 ${accepted}건 · 거부 ${added.length - accepted}건`;
      } catch (error) {
        console.warn('MVP upload failed', error);
        status.dataset.error = '';
        status.textContent = '업로드 결과를 확인할 수 없습니다. 페이지를 새로고침해 주세요.';
      } finally {
        const empty = !rows.children.length;
        document.querySelector('#mvp-empty').hidden = !empty;
        document.querySelector('#mvp-file-list').hidden = empty;
        start.disabled = !rows.querySelector('[data-file-status="ok"]');
        clear.disabled = empty;
        input.value = '';
        uploading = input.disabled = false;
      }
    }

    form.addEventListener('submit', event => { event.preventDefault(); upload(input.files); });
    input.addEventListener('change', () => upload(input.files));
    zone.addEventListener('dragover', event => {
      event.preventDefault();
      zone.classList.add('is-dragging');
    });
    zone.addEventListener('dragleave', event => {
      if (!zone.contains(event.relatedTarget)) zone.classList.remove('is-dragging');
    });
    zone.addEventListener('drop', event => {
      event.preventDefault();
      zone.classList.remove('is-dragging');
      upload(event.dataTransfer.files);
    });
  }

  const filters = document.querySelector('.mvp-filters');
  if (filters) {
    const query = filters.querySelector('#finding-query');
    const severity = filters.querySelector('#finding-severity');
    const status = filters.querySelector('#finding-status');
    const docSelect = filters.querySelector('#finding-doc');
    const matches = filters.querySelector('#finding-matches');
    const empty = document.querySelector('.mvp-no-matches');
    const normalize = value => value.normalize('NFC').toLowerCase().replace(/\s+/g, ' ');
    const entries = Array.from(document.querySelectorAll('.mvp-finding'), node => ({
      node, text: normalize(`${node.textContent} ${node.dataset.keywords || ''}`)
    }));
    const groups = document.querySelectorAll('[aria-labelledby="deductions-heading"] details.mvp-details');
    const sections = document.querySelectorAll('[aria-labelledby="deductions-heading"], [aria-labelledby="notes-heading"]');
    const toggles = document.querySelectorAll('[data-filter-severity], [data-filter-status]');
    const openBeforeSearch = new Map();
    function updateCount(container) {
      const count = container.querySelectorAll('.mvp-finding:not([hidden])').length;
      const label = container.querySelector('[data-finding-count]');
      const text = `${count}건`;
      if (label.textContent !== text) label.textContent = text;
      return count;
    }

    function applyFilters() {
      const terms = normalize(query.value).trim().split(' ').filter(Boolean);
      const active = Boolean(terms.length || severity.value || status.value || docSelect.value);
      let visible = 0;
      for (const {node, text} of entries) {
        node.hidden = Boolean((severity.value && node.dataset.severity !== severity.value)
          || (status.value && node.dataset.status !== status.value)
          || (docSelect.value && node.dataset.doc !== docSelect.value)
          || !terms.every(term => text.includes(term)));
        if (!node.hidden) visible++;
      }
      for (const group of groups) {
        if (active && !openBeforeSearch.has(group)) openBeforeSearch.set(group, group.open);
        const count = updateCount(group);
        group.hidden = active && !count;
        if (active) group.open = !group.hidden;
        else if (openBeforeSearch.has(group)) group.open = openBeforeSearch.get(group);
      }
      if (!active) openBeforeSearch.clear();
      for (const section of sections) {
        const count = updateCount(section);
        section.hidden = active && !count;
      }
      for (const button of toggles) {
        const pressed = button.dataset.filterSeverity
          ? severity.value === button.dataset.filterSeverity
          : status.value === button.dataset.filterStatus;
        button.setAttribute('aria-pressed', String(pressed));
      }
      const result = `전체 ${entries.length}건 중 ${visible}건 표시`;
      if (matches.textContent !== result) matches.textContent = result;
      empty.hidden = !active || visible > 0;
    }

    function resetFilters() {
      query.value = severity.value = status.value = docSelect.value = '';
      applyFilters();
    }

    filters.hidden = false;
    filters.addEventListener('input', event => {
      if (!event.isComposing) applyFilters();
    });
    query.addEventListener('compositionend', applyFilters);
    query.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.isComposing || !query.value) return;
      event.preventDefault();
      query.value = '';
      applyFilters();
    });
    for (const button of toggles) {
      button.addEventListener('click', () => {
        if (button.dataset.filterSeverity) {
          severity.value = severity.value === button.dataset.filterSeverity ? '' : button.dataset.filterSeverity;
        } else {
          status.value = status.value === button.dataset.filterStatus ? '' : button.dataset.filterStatus;
        }
        applyFilters();
        if (!filters.contains(button)) {
          filters.scrollIntoView({block: 'start'});
          query.focus({preventScroll: true});
        }
      });
    }
    filters.querySelector('#finding-reset').addEventListener('click', () => {
      resetFilters();
      query.focus({preventScroll: true});
    });
    empty.querySelector('[data-reset-filters]').addEventListener('click', () => {
      resetFilters();
      query.focus();
    });
    for (const link of document.querySelectorAll('.mvp-result-nav a[href="#deductions-heading"], .mvp-result-nav a[href="#notes-heading"]')) {
      link.addEventListener('click', () => {
        if (document.querySelector(link.hash).closest('section').hidden) resetFilters();
      });
    }
    applyFilters();
  }

  const viewToggle = document.querySelector('[data-view-toggle]');
  if (viewToggle) {
    const panels = document.querySelectorAll('[data-view-panel]');
    let saved = 'table';
    try { saved = localStorage.getItem('irb-summary-view') || 'table'; } catch (error) { /* storage blocked */ }
    function show(view) {
      for (const button of viewToggle.querySelectorAll('[data-view]')) {
        button.setAttribute('aria-pressed', String(button.dataset.view === view));
      }
      for (const panel of panels) panel.hidden = view !== 'both' && panel.dataset.viewPanel !== view;
      try { localStorage.setItem('irb-summary-view', view); } catch (error) { /* storage blocked */ }
    }
    viewToggle.hidden = false;
    viewToggle.addEventListener('click', event => {
      const button = event.target.closest('[data-view]');
      if (button) show(button.dataset.view);
    });
    show(['table', 'chart', 'both'].includes(saved) ? saved : 'table');
  }

  const scoreSummary = document.querySelector('#score-summary');
  if (scoreSummary) {
    window.addEventListener('pageshow', event => {
      if (!event.persisted && !window.location.hash && performance.getEntriesByType('navigation')[0]?.type === 'navigate') {
        scoreSummary.scrollIntoView({block: 'start'});
      }
    });
  }

  // 문서 뷰어는 현재 화면을 바꾸지 않고 화면 오른쪽 절반에 별도 창으로 연다(같은 창을 재사용).
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-document-viewer]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const screen = window.screen;
    const width = Math.round(Math.max(480, Math.min(1100, screen.availWidth * 0.5)));
    const left = (screen.availLeft || 0) + screen.availWidth - width;
    const features = `popup=yes,width=${width},height=${screen.availHeight},left=${left},top=${screen.availTop || 0}`;
    const popup = window.open(link.href, 'irb-document-viewer', features);
    if (popup) {
      popup.focus();
      event.preventDefault();
    }
  });

  if (document.body.classList.contains('viewer-body')) {
    const docSelect = document.querySelector('[data-viewer-doc]');
    const viewerMain = document.querySelector('.viewer-main');
    const frame = document.querySelector('.viewer-frame');
    const evidence = document.querySelector('.viewer-evidence-image');
    function showEvidence(src) {
      frame.hidden = true;
      evidence.src = src;
      evidence.hidden = false;
      viewerMain.scrollTop = 0;
    }

    if (frame?.hasAttribute('data-evidence-image')) {
      showEvidence(frame.src);
      frame.removeAttribute('src');
    }
    if (docSelect) {
      docSelect.form.querySelector('[data-viewer-doc-submit]').hidden = true;
      docSelect.addEventListener('change', () => docSelect.form.submit());
    }
    document.addEventListener('click', event => {
      const target = event.target.closest('[data-viewer-target]');
      if (!target || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      for (const item of document.querySelectorAll('.viewer-item[aria-current]')) item.removeAttribute('aria-current');
      target.closest('.viewer-item').setAttribute('aria-current', 'true');
      if (target.hasAttribute('data-evidence-image')) {
        event.preventDefault();
        showEvidence(target.href);
      } else {
        evidence.hidden = true;
        frame.hidden = false;
      }
      if (window.matchMedia('(max-width: 720px)').matches) {
        viewerMain.scrollIntoView({block: 'start'});
      }
    });
    const close = document.querySelector('[data-viewer-close]');
    if (close && window.opener) {
      close.hidden = false;
      close.addEventListener('click', () => window.close());
    }
    document.querySelector('.viewer-item[aria-current]')?.scrollIntoView({block: 'nearest'});
  }

  const review = document.querySelector('[data-review-status]');
  if (review) {
    const stage = document.querySelector('#review-stage');
    const failure = document.querySelector('#review-poll-error');
    async function poll() {
      try {
        const response = await fetch(review.dataset.reviewStatus, { cache: 'no-store' });
        if (response.status === 401) {
          window.location.assign('/login');
          return;
        }
        if (response.ok) {
          const result = await response.json();
          if (['done', 'failed'].includes(result.status)) {
            window.location.reload();
            return;
          }
          if (stage.textContent !== result.stage) stage.textContent = result.stage;
          failure.hidden = true;
        } else {
          failure.hidden = false;
        }
      } catch (error) {
        console.warn('MVP status request failed', error);
        failure.hidden = false;
      }
      window.setTimeout(poll, 1500);
    }
    window.setTimeout(poll, 1500);
  }
})();
