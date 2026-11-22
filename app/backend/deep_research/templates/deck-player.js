
(function(){
  var slides=[].slice.call(document.querySelectorAll('.slide'));
  var deck=document.getElementById('deck');
  var notes=document.getElementById('notes');
  var i=0, notesOn=false;

  function fit(){
    var pad=notesOn?0.80:0.94;
    var s=Math.min(window.innerWidth/1280, window.innerHeight/720)*pad;
    deck.style.transform='scale('+s+')';
  }
  function show(n){
    i=Math.max(0,Math.min(slides.length-1,n));
    slides.forEach(function(el,k){el.classList.toggle('active',k===i);});
    var note=slides[i].getAttribute('data-note')||'（本頁無講稿）';
    notes.innerHTML='<b>講稿 '+(i+1)+' / '+slides.length+'</b>';
    notes.appendChild(document.createTextNode(note));
    var bar=slides[i].querySelector('.progress');
    if(bar) bar.style.width=((i+1)/slides.length*100)+'%';
    try { location.hash='p'+(i+1); } catch { /* Opaque sandbox previews cannot always update blob URLs. */ }
  }
  function toggleNotes(){notesOn=!notesOn;notes.classList.toggle('on',notesOn);fit();}

  document.addEventListener('keydown',function(e){
    if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){e.preventDefault();show(i+1);}
    else if(e.key==='ArrowLeft'||e.key==='PageUp'){e.preventDefault();show(i-1);}
    else if(e.key==='Home'){show(0);}
    else if(e.key==='End'){show(slides.length-1);}
    else if(e.key==='n'||e.key==='N'){toggleNotes();}
    else if(e.key==='f'||e.key==='F'){
      if(document.fullscreenElement){document.exitFullscreen();}
      else{document.documentElement.requestFullscreen();}
    }
  });
  document.getElementById('stage').addEventListener('click',function(e){
    show(e.clientX < window.innerWidth/2 ? i-1 : i+1);
  });
  window.addEventListener('resize',fit);

  var m=(location.hash||'').match(/^#p(\d+)$/);
  fit(); show(m ? parseInt(m[1],10)-1 : 0);
})();
