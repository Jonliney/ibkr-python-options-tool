(() => {
  document.querySelectorAll('[data-interactive-price]').forEach(root => {
    if (root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    const svg = root.querySelector('svg');
    const points = [...svg.querySelectorAll('[data-price-point]')];
    const tooltip = root.querySelector('[data-price-tooltip]');
    const error = root.querySelector('[data-scenario-error]');
    const basis = Number(root.dataset.scenarioEntry);
    const baseLow = Number(root.dataset.scenarioLow);
    const baseSpread = Number(root.dataset.scenarioSpread);
    const money = value => new Intl.NumberFormat('en-GB', {
      style:'currency', currency:root.dataset.scenarioCurrency, currencyDisplay:'narrowSymbol'
    }).format(value);
    const bars = points.map(point => ({
      point, low:baseLow + Number(point.dataset.lowFraction)*baseSpread,
      high:baseLow + Number(point.dataset.highFraction)*baseSpread,
      close:baseLow + Number(point.dataset.yFraction)*baseSpread,
      timestamp:Number(point.dataset.timestamp), date:point.dataset.dateLabel
    }));
    const fields = Object.fromEntries(['stop','target'].map(kind =>
      [kind, root.querySelector(`[data-scenario-input="${kind}"]`)]));
    const levels = {stop:Number(fields.stop.value), target:Number(fields.target.value)};
    const committed = {...levels};
    const labels = Object.fromEntries(['stop','target'].map(kind =>
      [kind, root.querySelector(`[data-scenario-drag="${kind}"]`)]));
    let selected = -1, drag = null, scale = null;
    const ns = 'http://www.w3.org/2000/svg';
    const make = (tag, attributes) => {
      const element = document.createElementNS(ns, tag);
      Object.entries(attributes).forEach(([key,value])=>element.setAttribute(key,value));
      svg.append(element); return element;
    };
    const lines = {}, hits = {}, leaders = {};
    ['stop','target'].forEach(kind => {
      lines[kind] = make('line', {class:`journal-scenario-line journal-scenario-${kind}`});
      leaders[kind] = make('line', {class:`journal-scenario-line journal-scenario-${kind}`});
      hits[kind] = make('line', {'data-scenario-drag':kind, class:'journal-scenario-hit'});
    });
    const extrema = ['low','high'].map(kind => {
      const bar = bars.reduce((best,item)=>
        (kind==='low' ? item.low<best.low : item.high>best.high) ? item : best);
      const mark = make('circle', {r:4, class:'journal-price-extreme',
        tabindex:0, role:'button', 'aria-label':`${kind==='low'?'Lowest':'Highest'} observed bid ${money(bar[kind])}, ${bar.date}`});
      const title = document.createElementNS(ns,'title');
      title.textContent=mark.getAttribute('aria-label'); mark.append(title);
      const label = make('text', {class:'journal-chart-label journal-extreme-label'});
      label.textContent=kind==='low'?'L':'H';
      mark.addEventListener('pointerenter',()=>select(bars.indexOf(bar)));
      mark.addEventListener('focus',()=>select(bars.indexOf(bar)));
      mark.addEventListener('keydown',event=>{
        if(event.key==='Escape'){hide();event.preventDefault();}
        if(event.key==='Enter'||event.key===' '){select(bars.indexOf(bar));event.preventDefault();}
      });
      return {kind,bar,mark,label};
    });
    function positionTooltip() {
      if (selected<0 || tooltip.hidden) return;
      const box=root.getBoundingClientRect(), point=points[selected].getBoundingClientRect();
      const x=point.left+point.width/2-box.left, y=point.top+point.height/2-box.top;
      tooltip.style.left=`${Math.max(0,Math.min(box.width-tooltip.offsetWidth,x-tooltip.offsetWidth/2))}px`;
      tooltip.style.top=`${y>=tooltip.offsetHeight+12?y-tooltip.offsetHeight-12:y+12}px`;
    }
    function hide() {
      tooltip.hidden=true;
      points.forEach(point=>{point.classList.remove('is-selected');point.setAttribute('aria-pressed','false');});
    }
    function select(index) {
      if(drag || (selected===index && !tooltip.hidden))return;
      selected=index;
      points.forEach((point,i)=>{
        point.classList.toggle('is-selected',i===index);
        point.setAttribute('aria-pressed',String(i===index));
        point.setAttribute('tabindex',i===index?'0':'-1');
      });
      const data=points[index].dataset;
      tooltip.querySelector('[data-price-date]').textContent=data.dateLabel;
      ['close','low','high','return'].forEach(key=>{
        tooltip.querySelector(`[data-price-${key}]`).textContent=data[key];
      });
      tooltip.querySelector('[data-price-return]').className=data.returnTone;
      tooltip.hidden=false; positionTooltip();
    }
    const price = kind => basis*(1+(kind==='stop'?-1:1)*levels[kind]/100);
    const valid = (kind,value) => Number.isFinite(value) && value>=.1 && value<=(kind==='stop'?99.9:10000);
    function render() {
      const {width,height}=svg.getBoundingClientRect(); if(!width||!height)return;
      const low=Math.min(baseLow,price('stop'));
      const high=Math.max(baseLow+baseSpread,price('target'));
      const padding=Math.max((high-low)*.12,.01);
      if(!drag)scale={low:Math.max(0,low-padding),high:high+padding};
      const span=scale.high-scale.low;
      const x=f=>12+Number(f)*(width-24);
      const y=v=>height-32-(v-scale.low)/span*(height-64);
      svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
      bars.forEach(bar=>{
        bar.point.setAttribute('cx',x(bar.point.dataset.xFraction));
        bar.point.setAttribute('cy',y(bar.close));
      });
      svg.querySelectorAll('[data-price-segment]').forEach(line=>{
        const segment=bars.filter(bar=>bar.point.dataset.segment===line.dataset.priceSegment);
        line.setAttribute('points',segment.map(bar=>`${x(bar.point.dataset.xFraction)},${y(bar.close)}`).join(' '));
        const range=svg.querySelector(`[data-price-range="${line.dataset.priceSegment}"]`);
        const upper=segment.map(bar=>`${x(bar.point.dataset.xFraction)} ${y(bar.high)}`);
        const lower=[...segment].reverse().map(bar=>`${x(bar.point.dataset.xFraction)} ${y(bar.low)}`);
        range.setAttribute('d',`M ${[...upper,...lower].join(' L ')} Z`);
      });
      const entry=svg.querySelector('[data-entry-fraction]');
      entry.setAttribute('x2',width-12);entry.setAttribute('y1',y(basis));entry.setAttribute('y2',y(basis));
      const exit=svg.querySelector('[data-exit-fraction]');
      if(exit){exit.setAttribute('x1',x(exit.dataset.exitFraction));exit.setAttribute('x2',x(exit.dataset.exitFraction));exit.setAttribute('y2',height-25);}
      extrema.forEach(({kind,bar,mark,label})=>{
        const px=x(bar.point.dataset.xFraction),py=y(bar[kind]);
        mark.setAttribute('cx',px);mark.setAttribute('cy',py);
        label.setAttribute('x',Math.min(width-22,Math.max(12,px+8)));
        label.setAttribute('y',py+(kind==='low'?18:-10));
      });
      // Move nearby labels apart while the leader retains the exact level.
      const labelHeight=Math.max(labels.stop.offsetHeight,labels.target.offsetHeight,26);
      const gap=labelHeight+5;
      let targetY=Math.max(16,y(price('target')));
      let stopY=Math.max(y(price('stop')),targetY+gap);
      if(stopY>height-18){targetY-=stopY-(height-18);stopY=height-18;}
      const offset=svg.getBoundingClientRect().top-root.getBoundingClientRect().top;
      ['stop','target'].forEach(kind=>{
        const py=y(price(kind)),labelY=kind==='stop'?stopY:targetY;
        [lines[kind],hits[kind]].forEach(line=>{
          line.setAttribute('x1',12);line.setAttribute('x2',width-12);
          line.setAttribute('y1',py);line.setAttribute('y2',py);
        });
        leaders[kind].setAttribute('x1',width-12);leaders[kind].setAttribute('x2',width-12);
        leaders[kind].setAttribute('y1',py);leaders[kind].setAttribute('y2',labelY);
        labels[kind].style.top=`${offset+labelY-labels[kind].offsetHeight/2}px`;
        root.querySelector(`[data-scenario-price="${kind}"]`).textContent=money(price(kind));
        const grip=root.querySelector(`[data-scenario-grip="${kind}"]`);
        grip.setAttribute('aria-label',`Hypothetical ${kind} ${money(price(kind))}, ${levels[kind]} percent. Use Up and Down arrows to move the price.`);
      });
      positionTooltip();
    }
    function update(kind,value) {
      levels[kind]=Math.round(value*10)/10;
      if(drag || document.activeElement!==fields[kind])fields[kind].value=String(levels[kind]);
      render();
    }
    Object.entries(fields).forEach(([kind,input])=>{
      input.addEventListener('focus',()=>{committed[kind]=levels[kind];hide();input.select();});
      input.addEventListener('input',()=>{
        const value=input.valueAsNumber,okay=valid(kind,value);
        input.setAttribute('aria-invalid',String(!okay));error.hidden=okay;
        error.textContent=okay?'':`Enter a ${kind} percentage from 0.1 to ${kind==='stop'?'99.9':'10000'}.`;
        if(okay)update(kind,value);
      });
      input.addEventListener('blur',()=>{
        if(valid(kind,input.valueAsNumber))committed[kind]=levels[kind];
        else update(kind,committed[kind]);
        input.value=String(levels[kind]);input.removeAttribute('aria-invalid');error.hidden=true;render();
      });
      input.addEventListener('keydown',event=>{
        if(event.key==='Enter'){event.preventDefault();if(valid(kind,input.valueAsNumber))input.blur();}
        if(event.key==='Escape'){event.preventDefault();update(kind,committed[kind]);input.value=String(committed[kind]);input.blur();}
      });
    });
    function finishDrag(event,cancel=false) {
      if(!drag || event.pointerId!==drag.id)return;
      const previous=drag;drag=null;
      if(cancel)update(previous.kind,previous.original);
      committed[previous.kind]=levels[previous.kind];
      if(previous.element.hasPointerCapture(event.pointerId))previous.element.releasePointerCapture(event.pointerId);
      root.classList.remove('is-dragging');render();
    }
    root.addEventListener('pointerdown',event=>{
      const handle=event.target.closest('[data-scenario-drag]');
      if(!handle || event.target.closest('input') || event.button!==0)return;
      event.preventDefault();
      Object.values(fields).forEach(input=>input.blur());
      hide();
      let kind=handle.dataset.scenarioDrag;
      if(handle.classList.contains('journal-scenario-hit')){
        const py=event.clientY-svg.getBoundingClientRect().top;
        kind=['stop','target'].reduce((best,key)=>
          Math.abs(Number(hits[key].getAttribute('y1'))-py)<Math.abs(Number(hits[best].getAttribute('y1'))-py)?key:best);
      }
      drag={kind,id:event.pointerId,original:levels[kind],startY:event.clientY,element:handle,scale:{...scale}};
      handle.setPointerCapture(event.pointerId);root.classList.add('is-dragging');
    });
    root.addEventListener('pointermove',event=>{
      if(!drag||event.pointerId!==drag.id)return;
      event.preventDefault();
      const height=svg.getBoundingClientRect().height;
      const originalPrice=basis*(1+(drag.kind==='stop'?-1:1)*drag.original/100);
      const movedPrice=originalPrice-(event.clientY-drag.startY)/(height-64)*(drag.scale.high-drag.scale.low);
      const percent=(drag.kind==='stop'?1-movedPrice/basis:movedPrice/basis-1)*100;
      update(drag.kind,Math.max(.1,Math.min(drag.kind==='stop'?99.9:10000,percent)));
    });
    root.addEventListener('pointerup',event=>finishDrag(event));
    root.addEventListener('pointercancel',event=>finishDrag(event,true));
    root.addEventListener('lostpointercapture',event=>finishDrag(event,true));
    document.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&drag){const current=drag;finishDrag({pointerId:current.id},true);event.preventDefault();}
    });
    root.addEventListener('keydown',event=>{
      const grip=event.target.closest('[data-scenario-grip]');
      if(!grip||!['ArrowUp','ArrowDown'].includes(event.key))return;
      event.preventDefault();const kind=grip.dataset.scenarioGrip;
      const direction=(event.key==='ArrowUp'?1:-1)*(kind==='stop'?-1:1);
      update(kind,Math.max(.1,Math.min(kind==='stop'?99.9:10000,levels[kind]+direction*(event.shiftKey?1:.1))));
      committed[kind]=levels[kind];
    });
    points.forEach((point,index)=>{
      point.addEventListener('pointerenter',()=>select(index));point.addEventListener('focus',()=>select(index));point.addEventListener('click',()=>select(index));
      point.addEventListener('keydown',event=>{
        let next=index;
        if(event.key==='Escape'){event.preventDefault();hide();return;}
        if(event.key==='ArrowRight')next=Math.min(points.length-1,index+1);
        else if(event.key==='ArrowLeft')next=Math.max(0,index-1);
        else if(event.key==='Home')next=0;else if(event.key==='End')next=points.length-1;
        else if(!['Enter',' '].includes(event.key))return;
        event.preventDefault();points[next].focus();select(next);
      });
    });
    const inspectPointer=event=>{
      if(drag || event.target.closest('[data-scenario-drag]')){hide();return;}
      if(event.target.closest('.journal-price-extreme'))return;
      const box=svg.getBoundingClientRect();
      const px=event.clientX-box.left,py=event.clientY-box.top;
      if(px<0||px>box.width||py<0||py>box.height){hide();return;}
      const fraction=(px-12)/Math.max(1,box.width-24);
      const nearest=points.reduce((best,point,i)=>Math.abs(Number(point.dataset.xFraction)-fraction)<Math.abs(Number(points[best].dataset.xFraction)-fraction)?i:best,0);
      // Inspect only a 10px corridor around an observed close or its connected
      // line segment. Never project a hover corridor across missing minutes.
      let distance=Math.hypot(px-Number(points[nearest].getAttribute('cx')),
        py-Number(points[nearest].getAttribute('cy')));
      const right=points.findIndex(point=>Number(point.getAttribute('cx'))>=px);
      if(right>0){
        const a=points[right-1],b=points[right];
        if(a.dataset.segment===b.dataset.segment){
          const ax=Number(a.getAttribute('cx')),bx=Number(b.getAttribute('cx'));
          const ay=Number(a.getAttribute('cy')),by=Number(b.getAttribute('cy'));
          const t=Math.max(0,Math.min(1,((px-ax)*(bx-ax)+(py-ay)*(by-ay))/((bx-ax)**2+(by-ay)**2)));
          distance=Math.min(distance,Math.hypot(px-(ax+t*(bx-ax)),py-(ay+t*(by-ay))));
        }
      }
      if(distance<=10)select(nearest);else hide();
    };
    root.addEventListener('pointermove',inspectPointer,true);root.addEventListener('mousemove',inspectPointer,true);root.addEventListener('pointerenter',inspectPointer);
    root.addEventListener('pointerleave',()=>{if(!root.contains(document.activeElement))hide();});
    root.addEventListener('focusout',event=>{if(!root.contains(event.relatedTarget))hide();});
    document.addEventListener('pointerdown',event=>{if(!root.contains(event.target))hide();});
    const observer=new ResizeObserver(render);observer.observe(svg);render();
    window.addEventListener('pagehide',()=>observer.disconnect(),{once:true});
  });
})();
