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
