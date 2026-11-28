(function () {
  try {
    var p = JSON.parse(localStorage.getItem('legislation-user-prefs') || 'null');
    // First-paint palette per theme, so the body never flashes the wrong theme.
    var FLASH = {
      dark: ['#120d17', '#f7eee2', '#c6f432'],
      light: ['#fbf6ee', '#1f1726', '#c6f432'],
      classic: ['#ffffff', '#000000', '#0000ee'],
      modern: ['#f8f8f8', '#333333', '#f32928'],
      'federal-register': ['#ffffff', '#2d2d31', '#1d3557'],
      terminal: ['#050805', '#33ff66', '#33ff66'],
      'laser-pointer': ['#0b0a10', '#f2eef7', '#ff4d4d'],
      'barristers-chambers': ['#2b1a10', '#f3e9d2', '#d4af37'],
      hairball: ['#f6ebdd', '#3b2a20', '#a8471a'],
      'comic-book': ['#fff7d6', '#111111', '#1e4fd8'],
      'classic-mac': ['#dddddd', '#000000', '#2b2bb8'],
      'modern-mac': ['#f5f5f7', '#1d1d1f', '#0066cc'],
      'windows-95': ['#c0c0c0', '#000000', '#000080'],
      'windows-xp': ['#ece9d8', '#000000', '#0b4fb3'],
      'windows-vista': ['#eef3f8', '#1e1e1e', '#1c5aa6'],
      simpsons: ['#5AB7F0', '#1A1A1A', '#FFD521'],
      squishy: ['#fdf0f7', '#3d1f33', '#b0166e']
    };
    var d = document.documentElement;
    var t = (p && typeof p === 'object' && p.theme) || 'dark';
    d.setAttribute('data-theme', t);
    var f = FLASH[t] || FLASH.dark;
    var s = d.style;
    s.setProperty('--color-bg', f[0]);
    s.setProperty('--color-text', f[1]);
    var accent = f[2];
    if ((t === 'dark' || t === 'light') && p && typeof p === 'object' && p.accent_color) accent = p.accent_color;
    s.setProperty('--color-accent', accent);
  } catch (e) {}
})();
