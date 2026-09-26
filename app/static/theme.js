// Theme before first paint (loaded without defer): the saved choice, else the system setting.
try {
  const root = document.documentElement;
  const saved = localStorage.getItem(root.dataset.storageNamespace + ':theme');
  if ((saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark') root.dataset.theme = 'dark';
} catch {}
