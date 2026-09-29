
// Preserve the sidebar's scroll position across the full-page navigation that
// opening a dialog triggers (/open returns a fresh Page(), so .side re-renders
// from scrollTop 0 — which made clicking a dialog near the bottom jump the menu
// back to the top). Save on scroll, restore on load, from sessionStorage.
(function(){
  var KEY = 'sk_side_scroll';
  var side = document.querySelector('.side');
  if(!side) return;
  var y = sessionStorage.getItem(KEY);
  if(y !== null) side.scrollTop = +y;   // this script tag is the last child of
                                        // .side, so the list already exists here
  side.addEventListener('scroll', function(){
    sessionStorage.setItem(KEY, side.scrollTop);
  }, {passive: true});
})();

(function(){
  if(window.__sidebarSel) return; window.__sidebarSel = true;
  var sel = new Set(), anchor = null;
  function links(){ return Array.prototype.slice.call(
    document.querySelectorAll('.side .conv-row a.conv')); }
  // rows the user can actually see (the filter hides the rest): a range select
  // must never reach into hidden rows, or Delete would remove dialogs off-screen
  function visibleLinks(){ return links().filter(function(a){ return a.offsetParent !== null; }); }
  function nameOf(a){ return decodeURIComponent(
    (a.getAttribute('href') || '').replace('/open?dialog=', '')); }
  function paint(){
    links().forEach(function(a){ a.closest('.conv-row').classList.toggle('sel', sel.has(nameOf(a))); });
    var bar = document.getElementById('selBar');
    if(bar){ bar.style.display = sel.size ? 'flex' : 'none';
             var c = document.getElementById('selCount');
             if(c) c.textContent = sel.size + ' selected'; }
  }
  function deleteDialogs(targets){
    if(!targets.length) return;
    if(!confirm('Delete ' + targets.length + ' dialog(s) and all their cells? This cannot be undone.')) return;
    var f = document.createElement('form'); f.method = 'POST'; f.action = '/dialog/delete-bulk';
    var i = document.createElement('input'); i.type = 'hidden'; i.name = 'names';
    i.value = JSON.stringify(targets);
    f.appendChild(i); document.body.appendChild(f); f.submit();
  }
  window.__clearDialogSel = function(){ sel.clear(); anchor = null; paint(); };
  window.__deleteDialogSel = function(){ deleteDialogs(Array.from(sel)); };
  document.addEventListener('click', function(e){
    // a row's ⋯ Delete: delete the whole selection if this row is part of it,
    // otherwise just this one.
    var del = e.target.closest && e.target.closest('.conv-del');
    if(del){
      e.preventDefault();
      var name = del.getAttribute('data-dialog');
      deleteDialogs(sel.size && sel.has(name) ? Array.from(sel) : [name]);
      return;
    }
    var a = e.target.closest && e.target.closest('.side .conv-row a.conv');
    if(!a || !(e.shiftKey || e.metaKey || e.ctrlKey)) return;   // plain click navigates
    e.preventDefault();
    var names = visibleLinks().map(nameOf), nm = nameOf(a), i = names.indexOf(nm);
    var ai = anchor === null ? -1 : names.indexOf(anchor);
    if(e.shiftKey && ai !== -1){
      var lo = Math.min(ai, i), hi = Math.max(ai, i);
      for(var k = lo; k <= hi; k++) sel.add(names[k]);
    } else {
      if(sel.has(nm)) sel.delete(nm); else sel.add(nm);
      anchor = nm;
    }
    paint();
  });
})();

// Filter the dialog tree as you type. Matches the full dialog path (so "audio"
// finds everything under an audio folder), hides folders with no match and the
// Recent list (it would only duplicate matches), and restores folders' own
// open/closed state when the box is cleared. Esc clears.
window.__filterDialogs = function(q){
  var side = document.querySelector('.side'); if(!side) return;
  q = (q || '').trim().toLowerCase();
  // a selection made under a different filter could include rows now hidden
  if(window.__clearDialogSel) window.__clearDialogSel();
  var recent = side.querySelector('.recent');
  if(recent) recent.style.display = q ? 'none' : '';
  var any = false;
  side.querySelectorAll('.folder').forEach(function(f){
    if(q){ if(f.dataset.wasOpen === undefined) f.dataset.wasOpen = f.open ? '1' : '0'; f.open = true; }
    else if(f.dataset.wasOpen !== undefined){ f.open = f.dataset.wasOpen === '1'; delete f.dataset.wasOpen; }
  });
  side.querySelectorAll('.conv-row').forEach(function(r){
    if(recent && recent.contains(r)) return;
    var a = r.querySelector('a.conv');
    var name = a ? decodeURIComponent((a.getAttribute('href') || '').replace('/open?dialog=', '')) : '';
    var hit = !q || name.toLowerCase().indexOf(q) !== -1;
    r.style.display = hit ? '' : 'none';
    if(hit) any = true;
  });
  // deepest folders first, so a parent sees its children's final visibility
  Array.prototype.slice.call(side.querySelectorAll('.folder')).reverse().forEach(function(f){
    var vis = Array.prototype.some.call(f.children, function(c){
      return (c.classList.contains('conv-row') || c.classList.contains('folder')) && c.style.display !== 'none';
    });
    f.style.display = (!q || vis) ? '' : 'none';
  });
  var none = document.getElementById('sideNoMatch');
  if(none) none.style.display = (q && !any) ? '' : 'none';
};
(function(){
  var box = document.getElementById('sideFilter'); if(!box) return;
  box.addEventListener('keydown', function(e){
    if(e.key === 'Escape'){ box.value = ''; window.__filterDialogs(''); box.blur(); e.stopPropagation(); }
    if(e.key === 'Enter'){                     // open the first visible match
      var first = Array.prototype.find.call(document.querySelectorAll('.side .conv-row'), function(r){
        return r.offsetParent !== null && !r.closest('.recent'); });
      var a = first && first.querySelector('a.conv'); if(a) location.href = a.href;
    }
  });
})();
