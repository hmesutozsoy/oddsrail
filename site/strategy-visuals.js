(() => {
  'use strict';
  const examples = {
    mm: ['Quote both sides', 'Example: two 20-share bids, each priced at 48 cents, below a 50-cent midpoint.'],
    fade: ['Fade sharp moves', 'Example: a price jump of 8 cents during a six-hour lookback. A future reversal is not shown.'],
    settle: ['High-probability outcomes', 'Example: the entry-price band runs from 90 to 96 cents per share.'],
    value: ['Trade my forecast', 'Example: a 65-cent forecast compared with a 60-cent market price, a gap of 5 cents.'],
    momentum: ['Follow momentum', 'Example: price rises from 40 to 50 cents over six hours, a move of 10 cents.']
  };
  const records = [];
  const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
  let scheduled = false;
  const element = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text) node.textContent = text; return node; };
  const theme = () => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  function path(record, extension) { return '/assets/strategy-gifs/' + record.id + '-' + theme() + '.' + extension + '?v=20260913-4'; }
  function wantsMotion(record) { return !record.paused && (!preference.matches || record.motionOverride); }
  function paint(record) {
    const rect = record.figure.getBoundingClientRect();
    const visible = !document.hidden && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
    const play = visible && wantsMotion(record) && !record.failed;
    // Replacing the source is what stops a GIF; CSS animation rules do not.
    if (visible || record.image.hasAttribute('src')) {
      const source = path(record, play ? 'gif' : 'png');
      if (record.image.getAttribute('src') !== source && !record.posterFailed) record.image.setAttribute('src', source);
    }
    record.button.textContent = wantsMotion(record) ? 'Pause' : 'Play';
    record.button.setAttribute('aria-label', (wantsMotion(record) ? 'Pause ' : 'Play ') + examples[record.id][0] + ' example animation');
  }
  function update() {
    scheduled = false;
    records.forEach(paint);
  }
  function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(update); } }
  function attach(container, id) {
    if (!examples[id]) return;
    const figure = element('figure','strategy-visual');
    const toolbar = element('div','strategy-visual-toolbar');
    const button = element('button','strategy-animation-control','Pause'); button.type = 'button'; toolbar.append(button);
    const media = element('div','strategy-visual-media');
    const image = element('img'); image.width = 720; image.height = 260; image.alt = examples[id][1]; image.decoding = 'async'; image.hidden = true;
    media.append(image);
    const caption = element('figcaption','','Illustrative example, independent of your settings below.');
    figure.append(toolbar,media,caption);container.append(figure);
    const record = {id,figure,image,button,caption,paused:false,motionOverride:false,failed:false,posterFailed:false}; records.push(record);
    image.addEventListener('load', () => { image.hidden = false; });
    image.addEventListener('error', () => {
      if (record.failed) { record.posterFailed = true; image.hidden = true; caption.textContent = 'This example could not be loaded.'; }
      else { record.failed = true; record.paused = true; image.src = path(record,'png'); }
      button.disabled = true;
    });
    button.addEventListener('click', () => {
      if (wantsMotion(record)) { record.paused = true; record.motionOverride = false; }
      else { record.paused = false; record.motionOverride = preference.matches; }
      paint(record);
    });
    observer?.observe(figure); schedule();
  }
  const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(schedule) : null;
  preference.addEventListener('change', () => { records.forEach(record => { record.motionOverride = false; }); update(); });
  new MutationObserver(schedule).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  const form = document.getElementById('setup-form');
  if (form) new MutationObserver(schedule).observe(form,{subtree:true,attributes:true,attributeFilter:['hidden']});
  document.addEventListener('visibilitychange', update);
  window.addEventListener('scroll',schedule,{passive:true});
  window.addEventListener('resize',schedule);
  window.OddsRailStrategyVisuals = {attach};
  update();
})();
