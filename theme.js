(() => {
  let saved = null;
  try { saved = localStorage.getItem('career-signal-theme'); } catch {}
  const theme = saved === 'dark' || saved === 'light' ? saved : 'light';
  document.documentElement.dataset.theme = theme;
  const color = document.querySelector('meta[name="theme-color"]');
  if (color) color.content = theme === 'dark' ? '#111714' : '#f7f8f5';
})();
