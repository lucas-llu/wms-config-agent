/* Local presentation action: find the evidence currently in view and return
   to its answer. No widget changes, network requests or global listeners. */
(() => {
  const button = document.getElementById("__WB_RETURN_ID__");
  if (!button) return;
  button.onclick = () => {
    const root = button.closest('[data-testid="stTabs"]');
    if (!root) return;
    const midpoint = (100 + Math.max(100, innerHeight - 140)) / 2;
    const expanded = Array.from(
      root.querySelectorAll('[class*="st-key-answer-anchor-"]')
    ).flatMap(answer => {
      const panel = answer.querySelector("details[open]");
      if (!panel || panel.getClientRects().length === 0) return [];
      const bounds = panel.getBoundingClientRect();
      const distance = midpoint < bounds.top ? bounds.top - midpoint
        : midpoint > bounds.bottom ? midpoint - bounds.bottom : 0;
      return [{answer, distance}];
    });
    expanded.sort((a, b) => a.distance - b.distance);
    const target = expanded[0]?.answer;
    if (target) {
      target.scrollIntoView({block: "start", behavior: "instant"});
      target.tabIndex = -1;
      target.focus({preventScroll: true});
    }
  };
})();
