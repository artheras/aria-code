import type { IndexedElement } from "./session.js";

/**
 * Walks light DOM + open Shadow DOM trees, returning Playwright shadow-piercing
 * selectors (`host >> inner`) for visible interactive elements.
 */
export const EXTRACT_ELEMENTS_SCRIPT = `(() => {
  const INTERACTIVE = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="combobox"], [contenteditable="true"]';
  const out = [];

  function localSelector(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('data-testid')) {
      return '[data-testid="' + el.getAttribute('data-testid').replace(/"/g, '\\\\"') + '"]';
    }
    if (el.getAttribute('name')) {
      return el.tagName.toLowerCase() + '[name="' + el.getAttribute('name').replace(/"/g, '\\\\"') + '"]';
    }
    const siblings = Array.from(el.parentElement ? el.parentElement.children : []).filter(c => c.tagName === el.tagName);
    const idx = siblings.indexOf(el) + 1;
    return el.tagName.toLowerCase() + ':nth-of-type(' + idx + ')';
  }

  function walk(root, prefix, inShadow) {
    if (out.length >= 80) return;
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (out.length >= 80) break;
      if (el.matches && el.matches(INTERACTIVE)) {
        const style = window.getComputedStyle(el);
        if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
          const r = el.getBoundingClientRect();
          if (r.width >= 2 && r.height >= 2 && r.bottom >= 0 && r.top <= window.innerHeight && r.right >= 0 && r.left <= window.innerWidth) {
            const seg = localSelector(el);
            const selector = prefix ? (prefix + ' >> ' + seg) : seg;
            const label = (
              el.getAttribute('aria-label') ||
              el.innerText ||
              el.getAttribute('placeholder') ||
              el.getAttribute('title') ||
              el.value ||
              ''
            ).trim().replace(/\\s+/g, ' ').slice(0, 120);
            out.push({
              selector,
              in_shadow_dom: inShadow,
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || el.tagName.toLowerCase(),
              label,
              rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
            });
          }
        }
      }
      if (el.shadowRoot) {
        const hostSel = localSelector(el);
        const nextPrefix = prefix ? (prefix + ' >> ' + hostSel) : hostSel;
        walk(el.shadowRoot, nextPrefix, true);
      }
    }
  }

  walk(document, '', false);
  return out;
})()`;

export const CLEAR_OVERLAY_SCRIPT =
  "(() => { const el = document.getElementById('__aria_cu_overlay__'); if (el) el.remove(); })()";

export function buildOverlayScript(
  elements: IndexedElement[],
  markElements: boolean,
  cursorPos: [number, number] | null,
): string {
  const payload = JSON.stringify({ elements, markElements, cursorPos });
  return `(cfg => {
    const prev = document.getElementById('__aria_cu_overlay__');
    if (prev) prev.remove();
    const root = document.createElement('div');
    root.id = '__aria_cu_overlay__';
    root.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;font-family:monospace;';
    if (cfg.markElements) {
      for (const el of cfg.elements) {
        if (!el.rect) continue;
        const color = el.sensitive ? '#dc2626' : '#2563eb';
        const box = document.createElement('div');
        box.style.cssText = 'position:fixed;border:2px solid ' + color + ';left:' + el.rect.x + 'px;top:' + el.rect.y + 'px;width:' + el.rect.w + 'px;height:' + el.rect.h + 'px;box-sizing:border-box;';
        const tag = document.createElement('div');
        tag.textContent = '#' + el.element_id;
        tag.style.cssText = 'position:absolute;top:-16px;left:0;background:' + color + ';color:#fff;font-size:11px;padding:1px 4px;border-radius:2px;line-height:14px;';
        box.appendChild(tag);
        root.appendChild(box);
      }
    }
    if (cfg.cursorPos) {
      const dot = document.createElement('div');
      dot.style.cssText = 'position:fixed;left:' + (cfg.cursorPos[0] - 8) + 'px;top:' + (cfg.cursorPos[1] - 8) + 'px;width:16px;height:16px;border-radius:50%;border:2px solid #10b981;background:rgba(16,185,129,0.25);';
      root.appendChild(dot);
    }
    document.documentElement.appendChild(root);
  })(${payload})`;
}
