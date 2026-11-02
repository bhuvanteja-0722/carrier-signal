(() => {
  let saved = null;
  try { saved = localStorage.getItem('career-signal-theme'); } catch {}
  // Start new visitors in dark mode; respect a theme they explicitly saved.
  const theme = saved === 'dark' || saved === 'light' ? saved : 'dark';
  document.documentElement.dataset.theme = theme;
  const color = document.querySelector('meta[name="theme-color"]');
  if (color) color.content = theme === 'dark' ? '#111714' : '#f7f8f5';
})();
