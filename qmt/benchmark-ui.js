(() => {
  const payload = document.getElementById('comparison-data');
  if (!payload) return;
  const host = document.getElementById('comparison-chart'), tip = document.getElementById('comparison-tip');
  const legend = document.getElementById('comparison-legend'), ns = 'http://www.w3.org/2000/svg';
  const colors = {strategy:'#246548', benchmark:'#447da7', cash:'#8a7964'};
  let series;
  try {
    series = JSON.parse(payload.textContent);
    if (!Array.isArray(series) || !series.length) throw Error();
    series = series.map(line => {
      if (!Array.isArray(line.points) || !line.points.length) throw Error();
      let previous = '';
      const points = line.points.map(point => {
        const day = String(point.day), value = Number(point.value);
        if (!/^\d{8}$/.test(day) || day <= previous || !Number.isFinite(value) || value <= 0) throw Error();
        const stamp = new Date(`${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6)}T00:00:00Z`);
        if (Number.isNaN(stamp.getTime()) || stamp.toISOString().slice(0,10).replaceAll('-','') !== day) throw Error();
        previous = day;
        return {day, value};
      });
      if (Math.abs(points[0].value - 1) > .00001) throw Error();
      return {...line, points, values:new Map(points.map(point=>[point.day,point.value])), enabled:true};
    });
    if (series.some(line=>line.points[0].day!==series[0].points[0].day || line.points.at(-1).day!==series[0].points.at(-1).day)) throw Error();
  } catch {
    host.textContent = '比较曲线日期、起点或净值数据无效；未绘制替代走势。';
    return;
  }
  const days = [...new Set(series.flatMap(line=>line.points.map(point=>point.day)))].sort();
  let selected = days.length - 1;
  function svgNode(tag, attrs, parent, text) {
    const node = document.createElementNS(ns, tag);
    for (const [key,value] of Object.entries(attrs)) node.setAttribute(key,String(value));
    if (text !== undefined) node.textContent = text;
    parent.appendChild(node); return node;
  }
  function draw() {
    host.replaceChildren(); tip.textContent = ''; host.onkeydown = null;
    const active = series.filter(line=>line.enabled);
    if (!active.length) { host.textContent = '选择至少一条曲线查看。'; return; }
    const values = active.flatMap(line=>line.points.map(point=>point.value));
    const minimum = Math.min(1,...values), maximum = Math.max(1,...values);
    const pad = Math.max((maximum-minimum)*.12,.02), low = minimum-pad, high = maximum+pad;
    const width = Math.max(600,host.clientWidth||1080), left = 58, right = width-45, top = 22, bottom = 264;
    const x = index=>left+(right-left)*index/Math.max(1,days.length-1), y = value=>bottom-(value-low)/(high-low)*(bottom-top);
    const svg = svgNode('svg',{viewBox:`0 0 ${width} 308`,role:'img','aria-label':'历史资产比较曲线，起点为一'},host);
    svgNode('title',{},svg,active.map(line=>line.label).join('、'));
    for (let i=0;i<=4;i++) {
      const value=low+(high-low)*i/4, yy=y(value);
      svgNode('line',{x1:left,x2:right,y1:yy,y2:yy,stroke:'#dce2d5','stroke-dasharray':'3 5'},svg);
      svgNode('text',{x:8,y:yy+4,fill:'#657766','font-size':12},svg,value.toFixed(2));
    }
    const ticks=[...new Set([0,1,2,3,4].map(i=>Math.round((days.length-1)*i/4)))];
    for (const index of ticks) svgNode('text',{x:x(index),y:291,fill:'#657766','font-size':11,'text-anchor':'middle'},svg,`${days[index].slice(0,4)}/${days[index].slice(4,6)}`);
    active.forEach(line => {
      let path='', connected=false;
      // A missing date breaks the curve; it is not an unchanged or interpolated value.
      days.forEach((day,index)=>{
        const value=line.values.get(day);
        if (value===undefined) { connected=false; return; }
        path+=`${connected?'L':'M'} ${x(index).toFixed(2)} ${y(value).toFixed(2)} `;
        connected=true;
      });
      svgNode('path',{d:path,fill:'none',stroke:colors[line.kind]||'#746793','stroke-width':line.kind==='strategy'?2.8:2,
        'stroke-dasharray':line.kind==='cash'?'6 5':'','data-series':String(line.label)},svg);
    });
    const cross=svgNode('line',{x1:x(selected),x2:x(selected),y1:top,y2:bottom,stroke:'#75806e','stroke-dasharray':'4 4'},svg);
    const describe=()=>{
      cross.setAttribute('x1',x(selected));cross.setAttribute('x2',x(selected));
      const day=days[selected], date=`${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6)}`;
      tip.textContent=date+' ｜ '+active.map(line=>{
        const value=line.values.get(day);
        return `${line.label}：${value===undefined?'当日数据缺失':`${value.toFixed(4)}（${((value-1)*100).toFixed(2)}%）`}`;
      }).join(' ｜ ');
    };
    describe();
    svg.addEventListener('pointermove',event=>{
      const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;
      const local=point.matrixTransform(svg.getScreenCTM().inverse());
      selected=Math.max(0,Math.min(days.length-1,Math.round((local.x-left)/(right-left)*(days.length-1))));describe();
    });
    host.onkeydown=event=>{
      if (event.key==='ArrowLeft'||event.key==='ArrowRight') {
        event.preventDefault();selected=Math.max(0,Math.min(days.length-1,selected+(event.key==='ArrowLeft'?-1:1)));describe();
      }
    };
  }
  series.forEach(line=>{
    const label=document.createElement('label'), checkbox=document.createElement('input'), text=document.createElement('span');
    checkbox.type='checkbox';checkbox.checked=true;
    text.textContent=String(line.label);text.style.color=colors[line.kind]||'#746793';
    checkbox.addEventListener('change',()=>{line.enabled=checkbox.checked;draw();});
    label.append(checkbox,text);legend.appendChild(label);
  });
  window.addEventListener('resize',draw);
  draw();
})();
