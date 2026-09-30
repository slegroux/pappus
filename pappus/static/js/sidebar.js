
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
    var body = f.querySelector(':scope > .folder-body') || f;
    var vis = Array.prototype.some.call(body.children, function(c){
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

// Sidebar editing, Claude-projects style: rename dialogs and groups in place,
// create a group, move a dialog to another group, drag to re-order. A group is
// its dialogs' name prefix; the order and collapsed state live in groups.json.
(function(){
  if(window.__sidebarEdit) return; window.__sidebarEdit = true;
  var side = document.querySelector('.side'); if(!side) return;
  function post(url, data){
    return fetch(url, {method: 'POST', body: new URLSearchParams(data)})
      .then(function(r){ return r.json().catch(function(){ return {ok: false, error: 'HTTP ' + r.status}; }); })
      .catch(function(){ return {ok: false, error: 'The app did not answer.'}; });
  }
  // After an edit, go to the dialog this tab should show (the server says which).
  // Never reload: the current URL may name a dialog that was just renamed, and
  // opening it again would recreate it empty.
  function done(res){
    if(!res || !res.ok){ alert((res && res.error) || 'That did not work.'); return; }
    location.href = res.open ? '/open?dialog=' + encodeURIComponent(res.open) : '/';
  }
  function parentOf(p){ var i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i); }
  // Names are relative to where you are: "sub/name" inside `p` is `p/sub/name`.
  function under(parent, v){ return parent ? parent + '/' + v : v; }
  function closeMenus(keep){
    side.querySelectorAll('details.conv-actions[open]').forEach(function(d){
      if(!(keep && d.contains(keep))) d.open = false; });
    var m = document.getElementById('grpMenu');
    if(m){ var b = document.querySelector('.grp-dots[aria-expanded="true"]'); if(b) b.setAttribute('aria-expanded', 'false'); m.remove(); }
  }
  // A text field floated over `anchor`: Enter or leaving the field commits,
  // Esc cancels. Nothing is sent when the value is unchanged.
  function edit(anchor, value, commit, opts){
    opts = opts || {};
    closeMenus();
    var r = anchor.getBoundingClientRect();
    var inp = document.createElement('input');
    inp.type = 'text'; inp.className = 'side-edit'; inp.value = value; inp.spellcheck = false;
    if(opts.list) inp.setAttribute('list', opts.list);
    if(opts.placeholder) inp.placeholder = opts.placeholder;
    inp.setAttribute('aria-label', opts.label || 'Name');
    function place(){
      var r = anchor.getBoundingClientRect();
      inp.style.left = r.left + 'px'; inp.style.top = r.top + 'px';
      inp.style.width = Math.max(r.width, 200) + 'px'; inp.style.height = Math.max(r.height, 30) + 'px';
    }
    place(); side.addEventListener('scroll', place, {passive: true});   // stay on the row
    document.body.appendChild(inp); inp.focus(); inp.select();
    var over = false;
    function finish(ok){
      if(over) return; over = true;
      side.removeEventListener('scroll', place);
      var v = inp.value.trim(); inp.remove();
      if(ok && v !== value && (v || opts.allowEmpty)) commit(v);
    }
    inp.addEventListener('keydown', function(e){
      e.stopPropagation();
      if(e.key === 'Enter'){ e.preventDefault(); finish(true); }
      if(e.key === 'Escape'){ e.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', function(){
      // switching windows is not "done": keep the field and come back to it
      if(!document.hasFocus()){ window.addEventListener('focus', function(){ if(!over) inp.focus(); }, {once: true}); return; }
      finish(true);
    });
  }
  function renameDialog(row){
    var full = row.dataset.dialog;
    edit(row, row.dataset.leaf, function(v){
      post('/dialog/rename-to', {old: full, new: under(parentOf(full), v)}).then(done);
    }, {label: 'Dialog name'});
  }
  function moveDialog(row){
    var full = row.dataset.dialog;
    edit(row, parentOf(full), function(v){
      post('/dialog/move', {dialog: full, group: v}).then(done);
    }, {list: 'groupList', placeholder: 'Group (empty: top level)', allowEmpty: true, label: 'Move to group'});
  }
  function renameGroup(folder){
    var path = folder.dataset.path;
    edit(folder.querySelector('summary'), folder.dataset.seg, function(v){
      post('/group/rename', {old: path, new: under(parentOf(path), v)}).then(done);
    }, {label: 'Group name'});
  }
  function newGroup(anchor, parent){
    edit(anchor, '', function(v){
      post('/group/new', {name: under(parent, v)}).then(done);
    }, {placeholder: parent ? 'New group in ' + parent : 'New group name', label: 'New group name'});
  }
  function groupMenu(btn){
    var open = btn.getAttribute('aria-expanded') === 'true';
    closeMenus(); if(open) return;
    var folder = btn.closest('.folder'), path = folder.dataset.path;
    var m = document.createElement('div'); m.id = 'grpMenu'; m.className = 'grp-menu'; m.setAttribute('role', 'menu');
    [['New dialog here', function(){ post('/group/new', {name: path}).then(done); }],
     ['New group inside', function(){ newGroup(folder.querySelector('summary'), path); }],
     ['Rename group', function(){ renameGroup(folder); }]].forEach(function(it){
      var b = document.createElement('button'); b.type = 'button'; b.textContent = it[0]; b.setAttribute('role', 'menuitem');
      b.addEventListener('click', function(e){ e.stopPropagation(); closeMenus(); it[1](); });
      m.appendChild(b);
    });
    var r = btn.getBoundingClientRect();
    m.style.top = (r.bottom + 4) + 'px'; m.style.left = Math.max(8, r.right - 190) + 'px';
    document.body.appendChild(m); btn.setAttribute('aria-expanded', 'true');
    var items = Array.prototype.slice.call(m.querySelectorAll('button'));
    m.addEventListener('keydown', function(e){          // arrows move, Esc returns, Tab leaves
      var i = items.indexOf(document.activeElement);
      if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
        e.preventDefault(); items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); }
      else if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); closeMenus(); btn.focus(); }
      else if(e.key === 'Tab'){ closeMenus(); }
    });
    if(items[0]) items[0].focus();
  }
  document.addEventListener('click', function(e){
    var t = e.target.closest ? e.target : null; if(!t) return;
    var b;
    if((b = t.closest('.grp-dots'))){ e.preventDefault(); e.stopPropagation(); groupMenu(b); return; }
    if((b = t.closest('.grp-new'))){ e.preventDefault(); newGroup(b.closest('.seclabel'), ''); return; }
    if((b = t.closest('.conv-rename'))){ e.preventDefault(); renameDialog(b.closest('.conv-row')); return; }
    if((b = t.closest('.conv-move'))){ e.preventDefault(); moveDialog(b.closest('.conv-row')); return; }
    if(!t.closest('#grpMenu')) closeMenus(t);        // leave the ⋯ being clicked to its own toggle
  });
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape') closeMenus(); });
  // Remember collapsed groups (not while the filter has opened everything).
  // Browsers fire 'toggle' for every open <details> as the page renders, so only
  // a change from the last saved state is sent.
  side.querySelectorAll('.folder').forEach(function(f){ f.dataset.savedOpen = f.open ? '1' : '0'; });
  side.addEventListener('toggle', function(e){
    var f = e.target;
    if(!f.classList || !f.classList.contains('folder')) return;
    var q = document.getElementById('sideFilter'); if(q && q.value.trim()) return;
    var now = f.open ? '1' : '0';
    if(now === f.dataset.savedOpen) return;
    f.dataset.savedOpen = now;
    post('/group/collapse', {path: f.dataset.path, collapsed: f.open ? 0 : 1});
  }, true);
  // Drag to re-order. Dialogs can also be dropped into another group (a move);
  // groups re-order within their parent only.
  if(!window.Sortable) return;
  function keys(el){
    return Array.prototype.filter.call(el.children, function(c){
      return c.classList.contains('conv-row') || c.classList.contains('folder'); })
      .map(function(c){ return c.classList.contains('folder') ? 'f:' + c.dataset.seg : 'd:' + c.dataset.leaf; });
  }
  side.querySelectorAll('.tree-root, .folder-body').forEach(function(el){
    Sortable.create(el, {
      group: {name: 'dialogs', pull: true, put: function(to, from, drag){
        return drag.classList.contains('conv-row') || to.el === from.el; }},
      draggable: '.conv-row, .folder', animation: 120, delay: 180, delayOnTouchOnly: true,
      forceFallback: true, fallbackTolerance: 4,     // mouse-driven drag: reliable in nested lists and on touch
      fallbackOnBody: true, swapThreshold: 0.6, emptyInsertThreshold: 8,
      ghostClass: 'drag-ghost', chosenClass: 'drag-chosen',
      filter: 'input, .conv-actions, .grp-dots', preventOnFilter: false,
      onEnd: function(evt){
        var item = evt.item, to = evt.to, from = evt.from;
        if(to === from && evt.oldIndex === evt.newIndex) return;
        function saveOrder(){ return post('/sidebar/order', {parent: to.dataset.parent, keys: JSON.stringify(keys(to))}); }
        if(to !== from && item.classList.contains('conv-row')){
          // move first; record the new place only if the move succeeded
          post('/dialog/move', {dialog: item.dataset.dialog, group: to.dataset.parent}).then(function(r){
            if(!r || !r.ok){ done(r); location.reload(); return; }
            saveOrder().then(function(){ done(r); });
          });
        } else { saveOrder(); }
      }
    });
  });
})();
