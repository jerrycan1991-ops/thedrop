/**
 * Applies the stored theme before first paint.
 *
 * This runs synchronously in <head>, ahead of any stylesheet-driven paint, which is
 * what prevents the flash of the wrong theme. It cannot be a React effect: effects run
 * after paint, and the flash has already happened by then.
 *
 * Order: explicit stored choice > system preference > dark (the brand default).
 */
const THEME_SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem('thedrop-theme');
    if (stored === 'light' || stored === 'dark') {
      document.documentElement.setAttribute('data-theme', stored);
    }
    // No stored choice: the CSS handles system preference and falls back to dark,
    // so we deliberately set nothing here.
  } catch (e) {
    // Private mode or blocked storage. The CSS default (dark) still applies.
  }
})();
`;

export function ThemeScript() {
  return <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />;
}
