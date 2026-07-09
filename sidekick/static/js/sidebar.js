
(function(){
  if(window.__sidebarSel) return; window.__sidebarSel = true;
  var sel = new Set(), anchor = null;
  function links(){ return Array.prototype.slice.call(
    document.querySelectorAll('.side .conv-row a.conv')); }
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
    var names = links().map(nameOf), nm = nameOf(a), i = names.indexOf(nm);
    if(e.shiftKey && anchor !== null){
      var lo = Math.min(anchor, i), hi = Math.max(anchor, i);
      for(var k = lo; k <= hi; k++) sel.add(names[k]);
    } else {
      if(sel.has(nm)) sel.delete(nm); else sel.add(nm);
      anchor = i;
    }
    paint();
  });
})();
