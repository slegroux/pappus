
window.buildTOC = function(){
  var list = document.getElementById('tocList');
  if(!list) return;
  // Scope to ONE #stream via getElementById: during an htmx outerHTML swap the
  // old #stream lingers briefly, and a '#stream …' selector matches headings
  // under BOTH, so the TOC would double. getElementById resolves a single node.
  var stream = document.getElementById('stream');
  var heads = stream ? stream.querySelectorAll(
    '.note-view h1,.note-view h2,.note-view h3,' +
    '.note-view h4,.note-view h5,.note-view h6') : [];
  list.innerHTML = '';
  if(!heads.length){
    var e = document.createElement('div'); e.className = 'toc-empty';
    e.textContent = 'No note headings yet — add a note with # headings.';
    list.appendChild(e); return;
  }
  heads.forEach(function(h, i){
    if(!h.id) h.id = 'toc-h-' + i;
    var a = document.createElement('a');
    a.className = 'toc-link toc-' + h.tagName.toLowerCase();
    a.textContent = h.textContent;
    a.href = '#' + h.id;
    a.addEventListener('click', function(ev){
      ev.preventDefault();
      h.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    list.appendChild(a);
  });
};
// Dim a toggle's icon when its panel is hidden, so what's collapsed is obvious
// at a glance — and one click on the dimmed icon brings the panel back.
window.syncToggles = function(){
  var app = document.querySelector('.app');
  if(!app) return;
  function set(id, hidden){
    var b = document.getElementById(id);
    if(b) b.classList.toggle('off', hidden);
  }
  set('tgl-side', app.classList.contains('no-side'));
  set('tgl-paper', app.classList.contains('no-paper'));
  set('tgl-toc', !app.classList.contains('toc-open'));
};
// Toggle a layout column class on .app and persist it; syncToggles() updates the
// matching icon's dimmed state. (TOC uses cls 'toc-open'; dialogs uses 'no-side'.)
window.toggleCol = function(cls, key){
  var app = document.querySelector('.app');
  if(!app) return;
  var on = app.classList.toggle(cls);
  try { localStorage.setItem(key, on ? '1' : '0'); } catch(e){}
  window.syncToggles();
};
(function(){
  var app = document.querySelector('.app');
  if(!app) return;
  function restore(key, cls, defOn){
    var v = null; try { v = localStorage.getItem(key); } catch(e){}
    if(v === null) v = defOn ? '1' : '0';
    app.classList.toggle(cls, v === '1');
  }
  // At phone width the dialogs panel is a full-width view that REPLACES the
  // notebook (see the max-width:700px block in app.css), so defaulting it shown
  // would land every phone visit on the dialog list instead of the notebook.
  // Default it hidden there; desktop is unchanged. Only the DEFAULT moves — an
  // explicit toggle is still remembered per device via localStorage.
  var narrow = false;
  try { narrow = window.matchMedia('(max-width: 700px)').matches; } catch(e){}
  restore('pappus_toc', 'toc-open', true);          // TOC default open
  restore('pappus_noside', 'no-side', narrow);      // dialogs: shown on desktop, hidden on phone
  restore('pappus_nopaper', 'no-paper', false);     // paper viewer default shown
  restore('pappus_paperhidden', 'paper-collapsed', false);  // paper text default shown
  window.syncToggles();
  window.buildTOC();
})();
// ---- drag-to-resize columns ------------------------------------------------
(function(){
  var app = document.querySelector('.app');
  if(!app) return;
  // For each handle: the CSS var it drives, the column it sizes, and the sign of
  // the drag (side/paper sit left of their handle -> +dx widens; toc sits right
  // of its handle -> -dx widens). min/max clamp the resulting width in px.
  var SPEC = {
    side:  {v:'--side-w',  el:'.side',  sign: 1, min:170, max:520},
    paper: {v:'--paper-w', el:'.paper', sign: 1, min:220, max:900},
    toc:   {v:'--toc-w',   el:'.toc',   sign:-1, min:160, max:520}
  };
  var KEY = 'pappus_colw';
  function load(){ try { return JSON.parse(localStorage.getItem(KEY)||'{}'); } catch(e){ return {}; } }
  function save(o){ try { localStorage.setItem(KEY, JSON.stringify(o)); } catch(e){} }
  var saved = load();
  Object.keys(SPEC).forEach(function(k){
    if(saved[k]) app.style.setProperty(SPEC[k].v, saved[k] + 'px');
  });
  var drag = null;
  document.addEventListener('mousedown', function(e){
    var g = e.target.closest && e.target.closest('.gutter');
    if(!g) return;
    var s = SPEC[g.getAttribute('data-resize')]; if(!s) return;
    var col = document.querySelector(s.el); if(!col) return;
    drag = {s:s, k:g.getAttribute('data-resize'), x:e.clientX,
            w:col.getBoundingClientRect().width, g:g};
    g.classList.add('dragging');
    document.body.classList.add('col-resizing');
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e){
    if(!drag) return;
    var w = drag.w + drag.s.sign * (e.clientX - drag.x);
    w = Math.max(drag.s.min, Math.min(drag.s.max, w));
    app.style.setProperty(drag.s.v, w + 'px');
  });
  document.addEventListener('mouseup', function(){
    if(!drag) return;
    var col = document.querySelector(drag.s.el);
    var o = load();
    o[drag.k] = Math.round(col.getBoundingClientRect().width);
    save(o);
    drag.g.classList.remove('dragging');
    document.body.classList.remove('col-resizing');
    drag = null;
  });
})();

// The topbar panel toggles are <span role="button" tabindex="0">: make Enter and
// Space activate them like a real button, so they work from the keyboard.
(function(){
  if(window.__tglKeys) return; window.__tglKeys = true;
  document.addEventListener('keydown', function(e){
    var t = e.target;
    if((e.key === 'Enter' || e.key === ' ') && t && t.getAttribute &&
       t.getAttribute('role') === 'button' && t.classList.contains('tgl')){
      e.preventDefault(); e.stopPropagation(); t.click();
    }
  }, true);
})();
