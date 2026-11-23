// Applies the saved light/dark choice before first paint (no flash). Loaded in <head>.
(function () { try { if (localStorage.getItem('pixel-theme') === 'light') document.documentElement.setAttribute('data-theme', 'light'); } catch (e) {} })();
