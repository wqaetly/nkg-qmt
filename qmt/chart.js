(() => {
  const payload = document.getElementById('market-chart-data');
  if (!payload) return;
  const data = JSON.parse(payload.textContent), select = document.getElementById('chart-symbol');
  const range = document.getElementById('chart-range'), host = document.getElementById('stock-chart');
  const tip = document.getElementById('chart-tip'), explanation = document.getElementById('chart-explanation');
  const ns = 'http://www.w3.org/2000/svg';
  const fmt = n => Number(n).toFixed(2);
  const labelDate = d => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`;
  function el(tag, attrs, parent, text) {
    const node = document.createElementNS(ns, tag);
    for (const [key,value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    parent.appendChild(node); return node;
  }
  function draw() {
    const code = select.value, stock = data.stocks[code] || {};
    const all = stock.candles || [], bars = all.slice(-Number(range.value));
    const assessment = data.assessments.find(a => a.symbol === code);
    const plans = data.plans.filter(p => p.symbol === code), fills = data.fills.filter(f => f.symbol === code);
    host.replaceChildren(); tip.textContent = ''; explanation.replaceChildren();
    const note = document.createElement('p');
    note.textContent = assessment ? `${assessment.state}：${assessment.reason}。${data.notes[code] || ''}` : '无自动策略评估记录';
    explanation.appendChild(note);
    const orders = document.createElement('p');
    orders.textContent = plans.length ? plans.slice(-8).map(p => `#${p.id} ${p.side==='buy'?'买入':'卖出'} ${p.quantity} 股，限价 ${p.limit_price}，有效日 ${labelDate(p.execute_date)}，${({pending:'待模拟成交',partial:'部分成交',filled:'已模拟成交',expired:'失效未成交',closed:'已关闭'})[p.status] || p.status}`).join('；') : '该股票尚无自动模拟订单；图中不补画交易。';
    explanation.appendChild(orders);
    if (stock.error || !bars.length) { host.textContent = stock.error || '尚未获取该股票 K 线。'; return; }
    const width = 1080, height = 440, left = 62, right = 1000, top = 32, bottom = 305, volTop = 339, volBottom = 401;
    const visibleDays = new Set(bars.map(b => b.day));
    const visibleFills = fills.filter(f => visibleDays.has(f.trade_date));
    let low = Math.min(...bars.map(b=>b.low),...visibleFills.map(f=>Number(f.price)));
    let high = Math.max(...bars.map(b=>b.high),...visibleFills.map(f=>Number(f.price)));
    const pad = Math.max((high-low)*.12, high*.005); low-=pad; high+=pad;
    const step = (right-left)/bars.length, x = i => left+(i+.5)*step;
    const y = price => bottom-(Number(price)-low)/(high-low)*(bottom-top);
    const svg = el('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':`${select.selectedOptions[0].textContent} 未复权日 K 线、成交量及自动模拟操作`},host);
    el('title',{},svg,`${labelDate(bars[0].day)} 至 ${labelDate(bars.at(-1).day)}，未复权真实行情`);
    for(let i=0;i<=4;i++) {
      const value=low+(high-low)*i/4, yy=y(value);
      el('line',{x1:left,y1:yy,x2:right,y2:yy,stroke:'#dce2d5','stroke-dasharray':'3 5'},svg);
      el('text',{x:8,y:yy+4,fill:'#657766','font-size':12},svg,fmt(value));
    }
    const maxVolume = Math.max(...bars.map(b=>b.vol),1), body = Math.max(2,step*.64);
    bars.forEach((b,i)=>{
      const color=b.close>=b.open?'#b94e45':'#258273';
      el('line',{x1:x(i),x2:x(i),y1:y(b.high),y2:y(b.low),stroke:color,'stroke-width':1.3},svg);
      el('rect',{x:x(i)-body/2,y:Math.min(y(b.open),y(b.close)),width:body,height:Math.max(1.2,Math.abs(y(b.open)-y(b.close))),fill:color},svg);
      const h=b.vol/maxVolume*(volBottom-volTop);
      el('rect',{x:x(i)-body/2,y:volBottom-h,width:body,height:Math.max(.4,h),fill:color,opacity:.55},svg);
      if(i===0 || i===bars.length-1 || i%Math.max(1,Math.floor(bars.length/5))===0)
        el('text',{x:x(i),y:427,fill:'#657766','font-size':11,'text-anchor':'middle'},svg,b.day.slice(4,6)+'/'+b.day.slice(6));
    });
    el('text',{x:left,y:330,fill:'#657766','font-size':11},svg,'成交量 / 手');
    const reference = Number(assessment?.reference_price);
    if(reference>low && reference<high) {
      el('line',{x1:left,y1:y(reference),x2:right,y2:y(reference),stroke:'#94762f','stroke-dasharray':'6 4'},svg);
      el('text',{x:right+6,y:y(reference)+4,fill:'#94762f','font-size':11},svg,`参考 ${fmt(reference)}`);
    }
    plans.filter(p=>visibleDays.has(p.signal_date)).forEach(p=>{
      const i=bars.findIndex(b=>b.day===p.signal_date), yy=y(bars[i].high)-12;
      const marker=el('circle',{cx:x(i),cy:yy,r:7,fill:'#fffef9',stroke:'#8c7438','stroke-width':2},svg);
      el('title',{},marker,`信号 #${p.id} ${p.side==='buy'?'买入':'卖出'}，限价 ${p.limit_price}，${p.quantity} 股；${p.note}`);
      el('text',{x:x(i)+10,y:yy+4,fill:'#8c7438','font-size':10},svg,`信${p.id}`);
    });
    visibleFills.forEach(f=>{
      const i=bars.findIndex(b=>b.day===f.trade_date), xx=x(i), yy=y(f.price), buy=f.side==='buy';
      const marker=el('path',{d:buy?`M ${xx} ${yy} l -6 11 h 12 Z`:`M ${xx} ${yy} l -6 -11 h 12 Z`,fill:buy?'#b23c32':'#157969',stroke:'#fff','stroke-width':1},svg);
      el('title',{},marker,`模拟${buy?'买入':'卖出'} ${f.quantity} 股 @ ${f.price}，费用 ${f.fee}；计划 #${f.plan_id}`);
      el('text',{x:xx+9,y:yy+(buy?14:-8),fill:buy?'#b23c32':'#157969','font-size':11},svg,`${buy?'买':'卖'} ${f.price}`);
    });
    const cross = el('line',{x1:0,x2:0,y1:top,y2:volBottom,stroke:'#587867','stroke-dasharray':'4 3',visibility:'hidden'},svg);
    const describe=i=>{
      const b=bars[i]; cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');
      const trades=fills.filter(f=>f.trade_date===b.day).map(f=>`模拟${f.side==='buy'?'买':'卖'} ${f.quantity} 股 @ ${f.price}`).join('；');
      const signals=plans.filter(p=>p.signal_date===b.day).map(p=>`信号 #${p.id} ${p.side==='buy'?'买':'卖'} ${p.quantity} 股，限价 ${p.limit_price}，仅 ${labelDate(p.execute_date)} 有效`).join('；');
      tip.textContent=`${labelDate(b.day)}　开 ${fmt(b.open)}　高 ${fmt(b.high)}　低 ${fmt(b.low)}　收 ${fmt(b.close)}　量 ${b.vol.toLocaleString()} 手${signals?' ｜ '+signals:''}${trades?' ｜ '+trades:''}`;
    };
    let index=bars.length-1; describe(index);
    requestAnimationFrame(()=>{host.scrollLeft=host.scrollWidth-host.clientWidth;});
    svg.addEventListener('pointermove',event=>{
      const p=svg.createSVGPoint();p.x=event.clientX;p.y=event.clientY;
      const local=p.matrixTransform(svg.getScreenCTM().inverse());
      index=Math.max(0,Math.min(bars.length-1,Math.floor((local.x-left)/step)));describe(index);
    });
    host.onkeydown=event=>{
      if(event.key==='ArrowLeft'||event.key==='ArrowRight') {
        event.preventDefault();index=Math.max(0,Math.min(bars.length-1,index+(event.key==='ArrowLeft'?-1:1)));describe(index);
      }
    };
  }
  select.addEventListener('change',draw); range.addEventListener('change',draw);
  window.addEventListener('resize',()=>requestAnimationFrame(()=>{host.scrollLeft=host.scrollWidth-host.clientWidth;}));
  document.querySelectorAll('[data-chart-symbol]').forEach(button=>button.addEventListener('click',()=>{
    select.value=button.dataset.chartSymbol;draw();document.getElementById('chart-panel').scrollIntoView({behavior:'smooth'});host.focus({preventScroll:true});
  }));
  draw();
})();
