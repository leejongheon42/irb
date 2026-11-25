// DataTransfer items must be captured synchronously while the drop event is active.
export function droppedFiles(transfer) {
  const items = Array.from(transfer.items || []).filter(item => item.kind === 'file');
  if (!items.length) return Array.from(transfer.files, file => ({file}));
  const roots = items.map(item => ({entry: item.webkitGetAsEntry?.(), file: item.getAsFile()}));
  return walkRoots(roots);
}

async function* walkRoots(roots) {
  for (const {entry, file} of roots) {
    if (entry) yield* walk(entry, '');
    else if (file) yield {file};
  }
}

async function* walk(entry, parent) {
  const path = parent ? `${parent}/${entry.name}` : entry.name;
  try {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      yield {file, path: parent ? path : ''};
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      while (true) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) yield* walk(child, path);
      }
    }
  } catch {
    yield {path, error: '파일이나 폴더를 읽을 수 없습니다.'};
  }
}
