import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis, Tooltip,
  ResponsiveContainer, RadialBarChart, RadialBar,
} from "recharts";
import {
  Shield, Zap, AlertTriangle, ArrowUpRight, ArrowDownRight,
  Radio, Clock, ChevronDown, ChevronUp, Globe, Activity,
} from "lucide-react";

/* ═══════════════════════════════════════════════════════════════
   DATA ENGINE
   ═══════════════════════════════════════════════════════════════ */

const VENUES = [
  { id:"hyperliquid", short:"HL",    chain:"HL L1",    tier:1, angle:0   },
  { id:"lighter",     short:"LTR",   chain:"ETH ZK",   tier:1, angle:40  },
  { id:"paradex",     short:"PDX",   chain:"Starknet", tier:1, angle:80  },
  { id:"drift",       short:"DFT",   chain:"Solana",   tier:1, angle:120 },
  { id:"dydx",        short:"DYDX",  chain:"Cosmos",   tier:1, angle:160 },
  { id:"aster",       short:"AST",   chain:"BNB",      tier:2, angle:200 },
  { id:"apex",        short:"APX",   chain:"zkLink",   tier:2, angle:240 },
  { id:"ethereal",    short:"ETH*",  chain:"Converge", tier:2, angle:280 },
  { id:"injective",   short:"INJ",   chain:"Cosmos",   tier:2, angle:320 },
];
const SYMS = ["BTC","ETH","SOL","ARB","DOGE","AVAX","LINK","SUI","PEPE","WIF"];
const rand = (a,b) => Math.random()*(b-a)+a;
const pick = a => a[Math.floor(Math.random()*a.length)];

function spark(n=24,base=0,vol=0.01){
  const p=[];let v=base;
  for(let i=0;i<n;i++){v+=(Math.random()-0.48)*vol;p.push({t:i,v});}return p;
}
function genVenues(){
  return VENUES.map(ve=>{
    const rates={};
    SYMS.forEach(s=>{if(Math.random()>0.1){
      const b=s==="BTC"?0.10:s==="ETH"?0.12:rand(0.04,0.35);
      rates[s]={ann:(b+rand(-0.08,0.15))*(Math.random()>0.2?1:-1),spark:spark(24,b,0.02)};
    }});
    return{...ve,score:rand(0.35,0.95),vol24h:rand(50,4000)*1e6,oi:rand(20,1500)*1e6,
      rates,status:Math.random()>0.06?"up":"degraded",latency:Math.round(rand(40,700)),
      fundingCollected:rand(10,800)};
  });
}
function genOpps(vd){
  const o=[];
  SYMS.forEach(s=>{
    const w=vd.filter(v=>v.rates[s]);if(w.length<2)return;
    const sr=[...w].sort((a,b)=>b.rates[s].ann-a.rates[s].ann);
    const hi=sr[0],lo=sr[sr.length-1],sp=hi.rates[s].ann-lo.rates[s].ann;
    if(sp>0.03)o.push({symbol:s,shortV:hi.id,shortN:hi.short,longV:lo.id,longN:lo.short,
      shortRate:hi.rates[s].ann,longRate:lo.rates[s].ann,spread:sp,
      net:sp-rand(0.005,0.02),conf:rand(0.5,0.95),spark:spark(24,sp,0.015)});
  });
  return o.sort((a,b)=>b.spread-a.spread);
}
function genPortfolio(n=72){
  let nav=100000;const p=[];
  for(let i=0;i<n;i++){nav+=(Math.random()-0.42)*400;
    p.push({t:Date.now()-(n-i)*3600000,nav:Math.round(nav),f:Math.round(rand(5,45))});}return p;
}
function genLogs(vd,opps){
  const msgs=[];const now=Date.now();
  const templates=[
    ()=>`Funding rates collected across ${vd.filter(v=>v.status==="up").length} venues`,
    ()=>{const o=opps[0];return o?`Spread detected: ${o.symbol} ${o.shortN}→${o.longN} ${(o.spread*100).toFixed(1)}%`:null;},
    ()=>`Risk check passed — drawdown ${rand(0.5,2.2).toFixed(1)}%, margin util ${rand(20,40).toFixed(0)}%`,
    ()=>`${pick(VENUES).short} heartbeat OK — ${Math.round(rand(50,300))}ms latency`,
    ()=>`Venue scorer updated — top: ${pick(VENUES).short} (${rand(0.7,0.95).toFixed(2)})`,
    ()=>`Circuit breaker check: INACTIVE — all limits within bounds`,
    ()=>`CoinGlass supplement: ${SYMS.slice(0,3).join(", ")} rates cross-validated`,
    ()=>{const v=pick(VENUES);return`${v.short} OI shifted ${rand(-5,8).toFixed(1)}% in last hour`;},
  ];
  for(let i=0;i<14;i++){
    const fn=pick(templates);const msg=fn();
    if(msg)msgs.push({ts:now-i*rand(4000,25000),msg,level:Math.random()>0.85?"warn":"info"});
  }
  return msgs.sort((a,b)=>b.ts-a.ts);
}

/* ═══════════════════════════════════════════════════════════════
   STYLES & TOKENS
   ═══════════════════════════════════════════════════════════════ */

const C={
  void:"#060609",panel:"#0c0c11",surface:"#101018",
  border:"rgba(255,255,255,0.04)",borderHi:"rgba(255,255,255,0.07)",
  cyan:"#22d3ee",cyanDim:"rgba(34,211,238,0.12)",cyanGlow:"rgba(34,211,238,0.25)",
  amber:"#f59e0b",amberDim:"rgba(245,158,11,0.12)",
  green:"#34d399",greenDim:"rgba(52,211,153,0.1)",
  red:"#f87171",redDim:"rgba(248,113,113,0.1)",
  indigo:"#818cf8",indigoDim:"rgba(129,140,248,0.1)",
  text:"#e4e4e7",textMid:"#71717a",textDim:"#3f3f46",textGhost:"#27272a",
};
const FONT=`'Outfit','Helvetica Neue',sans-serif`;
const MONO=`'IBM Plex Mono','SF Mono',monospace`;

/* ═══════════════════════════════════════════════════════════════
   MICRO-COMPONENTS
   ═══════════════════════════════════════════════════════════════ */

const Pct=({v,mono=true})=>{
  const pos=v>=0;
  return <span style={{color:pos?C.green:C.red,fontFamily:mono?MONO:"inherit",fontSize:12,fontWeight:500,
    display:"inline-flex",alignItems:"center",gap:2}}>
    {pos?<ArrowUpRight size={11}/>:<ArrowDownRight size={11}/>}
    {pos?"+":""}{v.toFixed(2)}%
  </span>;
};
const Mono=({children,style={}})=><span style={{fontFamily:MONO,fontSize:12,...style}}>{children}</span>;
const USD=v=>{if(v>=1e9)return`$${(v/1e9).toFixed(2)}B`;if(v>=1e6)return`$${(v/1e6).toFixed(1)}M`;
  if(v>=1e3)return`$${(v/1e3).toFixed(1)}K`;return`$${v.toFixed(0)}`;};

const Glow=({color=C.cyan,size=120,opacity=0.07,style={}})=>(
  <div style={{position:"absolute",width:size,height:size,borderRadius:"50%",
    background:`radial-gradient(circle,${color} 0%,transparent 70%)`,opacity,
    filter:"blur(40px)",pointerEvents:"none",...style}}/>
);

const GlassCard=({children,style={},glow=false,glowColor=C.cyan})=>(
  <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:14,
    position:"relative",overflow:"hidden",...style}}>
    {glow&&<Glow color={glowColor} size={180} opacity={0.05} style={{top:-60,right:-60}}/>}
    {children}
  </div>
);

const SortTh=({label,k,sort,onSort,align="left"})=>{
  const active=sort.key===k;
  return <th onClick={()=>onSort(k)} style={{textAlign:align,padding:"10px 14px",cursor:"pointer",
    userSelect:"none",color:active?C.textMid:C.textDim,fontWeight:500,fontSize:10,
    letterSpacing:"0.08em",textTransform:"uppercase",borderBottom:`1px solid ${C.border}`,
    background:C.surface,position:"sticky",top:0,zIndex:2,whiteSpace:"nowrap",fontFamily:FONT}}>
    <span style={{display:"inline-flex",alignItems:"center",gap:3}}>
      {label}{active&&(sort.asc?<ChevronUp size={10}/>:<ChevronDown size={10}/>)}
    </span>
  </th>;
};

const MiniSpark=({data,color=C.cyan,w=64,h=20})=>(
  <ResponsiveContainer width={w} height={h}>
    <LineChart data={data} margin={{top:2,bottom:2,left:0,right:0}}>
      <Line type="monotone" dataKey="v" stroke={color} strokeWidth={1.5} dot={false} isAnimationActive={false}/>
    </LineChart>
  </ResponsiveContainer>
);

/* ═══════════════════════════════════════════════════════════════
   ORBITAL VENUE MAP — the centrepiece
   ═══════════════════════════════════════════════════════════════ */

const OrbitalMap=({venues,opps})=>{
  const canvasRef=useRef(null);
  const frameRef=useRef(0);

  useEffect(()=>{
    const cvs=canvasRef.current;if(!cvs)return;
    const ctx=cvs.getContext("2d");
    const dpr=window.devicePixelRatio||1;
    const W=cvs.clientWidth,H=cvs.clientHeight;
    cvs.width=W*dpr;cvs.height=H*dpr;
    ctx.scale(dpr,dpr);

    const cx=W/2,cy=H/2,R=Math.min(W,H)*0.38;
    const activeOpps=opps.slice(0,5);

    const draw=(t)=>{
      ctx.clearRect(0,0,W,H);

      // Outer ring
      ctx.beginPath();ctx.arc(cx,cy,R+8,0,Math.PI*2);
      ctx.strokeStyle="rgba(34,211,238,0.06)";ctx.lineWidth=1;ctx.stroke();
      ctx.beginPath();ctx.arc(cx,cy,R,0,Math.PI*2);
      ctx.strokeStyle="rgba(34,211,238,0.03)";ctx.lineWidth=24;ctx.stroke();

      // Inner ring
      ctx.beginPath();ctx.arc(cx,cy,R*0.45,0,Math.PI*2);
      ctx.strokeStyle="rgba(255,255,255,0.02)";ctx.lineWidth=1;ctx.stroke();

      // Connection arcs for active hedges
      activeOpps.forEach((opp,i)=>{
        const sv=venues.find(v=>v.id===opp.shortV);
        const lv=venues.find(v=>v.id===opp.longV);
        if(!sv||!lv)return;
        const sa=((sv.angle-90)*Math.PI)/180;
        const la=((lv.angle-90)*Math.PI)/180;
        const sx=cx+Math.cos(sa)*R,sy=cy+Math.sin(sa)*R;
        const lx=cx+Math.cos(la)*R,ly=cy+Math.sin(la)*R;

        // Arc line
        ctx.beginPath();ctx.moveTo(sx,sy);
        const mx=(sx+lx)/2+(cy-sy)*0.15,my=(sy+ly)/2+(sx-cx)*0.15;
        ctx.quadraticCurveTo(mx,my,lx,ly);
        ctx.strokeStyle=`rgba(34,211,238,${0.08+Math.sin(t/1000+i)*0.04})`;
        ctx.lineWidth=1.5;ctx.stroke();

        // Traveling pulse
        const prog=((t/3000+i*0.2)%1);
        const px=(1-prog)*(1-prog)*sx+2*(1-prog)*prog*mx+prog*prog*lx;
        const py=(1-prog)*(1-prog)*sy+2*(1-prog)*prog*my+prog*prog*ly;
        ctx.beginPath();ctx.arc(px,py,2.5,0,Math.PI*2);
        ctx.fillStyle=`rgba(34,211,238,${0.6+Math.sin(t/400)*0.3})`;ctx.fill();
        ctx.beginPath();ctx.arc(px,py,8,0,Math.PI*2);
        ctx.fillStyle="rgba(34,211,238,0.08)";ctx.fill();
      });

      // Venue nodes
      venues.forEach((v)=>{
        const a=((v.angle-90)*Math.PI)/180;
        const x=cx+Math.cos(a)*R,y=cy+Math.sin(a)*R;
        const isUp=v.status==="up";
        const pulse=Math.sin(t/800+v.angle)*0.3+0.7;

        // Outer glow
        if(isUp){
          ctx.beginPath();ctx.arc(x,y,16,0,Math.PI*2);
          ctx.fillStyle=`rgba(34,211,238,${0.04*pulse})`;ctx.fill();
        }

        // Node circle
        ctx.beginPath();ctx.arc(x,y,7,0,Math.PI*2);
        ctx.fillStyle=isUp?C.surface:"#1a0a0a";
        ctx.fill();
        ctx.strokeStyle=isUp?`rgba(34,211,238,${0.5*pulse})`:"rgba(248,113,113,0.4)";
        ctx.lineWidth=1.5;ctx.stroke();

        // Inner dot
        ctx.beginPath();ctx.arc(x,y,2.5,0,Math.PI*2);
        ctx.fillStyle=isUp?C.cyan:C.red;ctx.fill();

        // Label
        ctx.font=`500 10px Outfit, sans-serif`;ctx.textAlign="center";
        ctx.fillStyle=isUp?"rgba(228,228,231,0.7)":"rgba(248,113,113,0.5)";
        const ly2=y+(v.angle>90&&v.angle<270?-16:18);
        ctx.fillText(v.short,x,ly2);
      });

      // Centre text
      ctx.font=`600 13px Outfit, sans-serif`;ctx.textAlign="center";
      ctx.fillStyle="rgba(228,228,231,0.5)";
      ctx.fillText("AEGIS",cx,cy-6);
      ctx.font=`500 9px IBM Plex Mono, monospace`;
      ctx.fillStyle="rgba(113,113,122,0.5)";
      ctx.fillText(`${venues.filter(v=>v.status==="up").length}/${venues.length} online`,cx,cy+10);

      frameRef.current=requestAnimationFrame(draw);
    };
    frameRef.current=requestAnimationFrame(draw);
    return()=>cancelAnimationFrame(frameRef.current);
  },[venues,opps]);

  return <canvas ref={canvasRef} style={{width:"100%",height:"100%",display:"block"}}/>;
};

/* ═══════════════════════════════════════════════════════════════
   RING GAUGE — for risk metrics
   ═══════════════════════════════════════════════════════════════ */

const RingGauge=({value,max,label,color=C.cyan,size=80})=>{
  const pct=Math.min(value/max,1);
  const r=size/2-6;const circ=2*Math.PI*r;
  const warn=pct>0.85;const mid=pct>0.6&&!warn;
  const c=warn?C.red:mid?C.amber:color;
  return(
    <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:6}}>
      <div style={{position:"relative",width:size,height:size}}>
        <svg width={size} height={size} style={{transform:"rotate(-90deg)"}}>
          <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="rgba(255,255,255,0.03)" strokeWidth={4}/>
          <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={c} strokeWidth={4}
            strokeDasharray={circ} strokeDashoffset={circ*(1-pct)} strokeLinecap="round"
            style={{transition:"stroke-dashoffset 0.8s ease, stroke 0.4s",filter:warn?`drop-shadow(0 0 6px ${C.redDim})`:"none"}}/>
        </svg>
        <div style={{position:"absolute",inset:0,display:"flex",alignItems:"center",justifyContent:"center"}}>
          <Mono style={{color:c,fontSize:13,fontWeight:600}}>{value.toFixed(value<1?2:1)}</Mono>
        </div>
      </div>
      <span style={{fontSize:9,color:C.textDim,fontWeight:500,textTransform:"uppercase",
        letterSpacing:"0.08em",textAlign:"center",fontFamily:FONT}}>{label}</span>
    </div>
  );
};

/* ═══════════════════════════════════════════════════════════════
   TERMINAL FEED
   ═══════════════════════════════════════════════════════════════ */

const TerminalFeed=({logs})=>{
  const ref=useRef(null);
  useEffect(()=>{if(ref.current)ref.current.scrollTop=0;},[logs]);
  return(
    <div ref={ref} style={{fontFamily:MONO,fontSize:11,lineHeight:1.7,maxHeight:280,overflowY:"auto",
      padding:"14px 16px",scrollbarWidth:"thin",scrollbarColor:`${C.textGhost} transparent`}}>
      {logs.map((l,i)=>(
        <div key={i} style={{display:"flex",gap:10,opacity:1-i*0.05,color:l.level==="warn"?C.amber:C.textDim}}>
          <span style={{color:C.textGhost,flexShrink:0}}>
            {new Date(l.ts).toLocaleTimeString("en-US",{hour12:false,hour:"2-digit",minute:"2-digit",second:"2-digit"})}
          </span>
          <span style={{color:l.level==="warn"?C.amber:C.textMid}}>{l.msg}</span>
        </div>
      ))}
    </div>
  );
};

/* ═══════════════════════════════════════════════════════════════
   HEATMAP
   ═══════════════════════════════════════════════════════════════ */

const Heatmap=({venues,syms=SYMS.slice(0,6)})=>{
  const vis=venues.filter(v=>v.status==="up").slice(0,7);
  const cellColor=(val)=>{
    if(val===null)return"rgba(255,255,255,0.015)";
    const a=Math.min(0.55,Math.abs(val)*2.2);
    return val>0?`rgba(52,211,153,${a})`:`rgba(248,113,113,${a})`;
  };
  return(
    <div style={{overflowX:"auto"}}>
      <table style={{width:"100%",borderCollapse:"collapse"}}>
        <thead><tr>
          <th style={{padding:"8px 12px",textAlign:"left",fontSize:10,color:C.textDim,fontWeight:500,
            textTransform:"uppercase",letterSpacing:"0.08em",borderBottom:`1px solid ${C.border}`,fontFamily:FONT}}>Venue</th>
          {syms.map(s=><th key={s} style={{padding:"8px 10px",textAlign:"center",fontSize:10,color:C.textDim,
            fontWeight:500,textTransform:"uppercase",letterSpacing:"0.08em",
            borderBottom:`1px solid ${C.border}`,fontFamily:FONT}}>{s}</th>)}
        </tr></thead>
        <tbody>{vis.map((v,i)=>(
          <tr key={v.id} style={{background:i%2===0?"transparent":"rgba(255,255,255,0.008)"}}>
            <td style={{padding:"8px 12px",borderBottom:`1px solid ${C.border}`,fontSize:12,
              fontWeight:500,color:C.textMid,fontFamily:FONT}}>{v.short}</td>
            {syms.map(s=>{
              const rate=v.rates[s]?.ann??null;
              return <td key={s} style={{padding:"5px 6px",textAlign:"center",borderBottom:`1px solid ${C.border}`}}>
                <div style={{padding:"3px 6px",borderRadius:4,display:"inline-block",minWidth:50,
                  background:cellColor(rate)}}>
                  {rate!==null?<Mono style={{color:rate>=0?"#a7f3d0":"#fecaca",fontSize:11}}>
                    {rate>=0?"+":""}{(rate*100).toFixed(1)}%</Mono>
                  :<span style={{color:C.textGhost,fontSize:11}}>—</span>}
                </div>
              </td>;
            })}
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
};

/* ═══════════════════════════════════════════════════════════════
   OPPORTUNITIES TABLE
   ═══════════════════════════════════════════════════════════════ */

const OppsTable=({opps})=>{
  const [sort,setSort]=useState({key:"spread",asc:false});
  const onSort=k=>setSort(s=>({key:k,asc:s.key===k?!s.asc:false}));
  const sorted=useMemo(()=>{
    const a=[...opps];a.sort((x,y)=>{
      let xv,yv;
      switch(sort.key){
        case"symbol":return sort.asc?x.symbol.localeCompare(y.symbol):y.symbol.localeCompare(x.symbol);
        case"spread":xv=x.spread;yv=y.spread;break;
        case"net":xv=x.net;yv=y.net;break;
        case"conf":xv=x.conf;yv=y.conf;break;
        default:xv=0;yv=0;
      }return sort.asc?xv-yv:yv-xv;
    });return a;
  },[opps,sort]);

  return(
    <div style={{overflowX:"auto"}}>
      <table style={{width:"100%",borderCollapse:"collapse"}}>
        <thead><tr>
          <SortTh label="Pair" k="symbol" sort={sort} onSort={onSort}/>
          <th style={{padding:"10px 14px",fontSize:10,color:C.textDim,fontWeight:500,textTransform:"uppercase",
            letterSpacing:"0.08em",borderBottom:`1px solid ${C.border}`,background:C.surface,textAlign:"left",fontFamily:FONT}}>Route</th>
          <SortTh label="Gross" k="spread" sort={sort} onSort={onSort} align="right"/>
          <th style={{padding:"10px 14px",fontSize:10,color:C.textDim,fontWeight:500,textTransform:"uppercase",
            letterSpacing:"0.08em",borderBottom:`1px solid ${C.border}`,background:C.surface,textAlign:"center",fontFamily:FONT}}>24h</th>
          <SortTh label="Net" k="net" sort={sort} onSort={onSort} align="right"/>
          <SortTh label="Conf" k="conf" sort={sort} onSort={onSort} align="right"/>
        </tr></thead>
        <tbody>{sorted.slice(0,8).map((o,i)=>(
          <tr key={`${o.symbol}-${i}`}
            style={{background:i%2===0?"transparent":"rgba(255,255,255,0.008)",transition:"background 0.1s"}}
            onMouseEnter={e=>e.currentTarget.style.background="rgba(34,211,238,0.02)"}
            onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"transparent":"rgba(255,255,255,0.008)"}>
            <td style={{padding:"10px 14px",borderBottom:`1px solid ${C.border}`}}>
              <span style={{fontWeight:600,color:C.text,fontSize:13,fontFamily:FONT}}>{o.symbol}</span>
              <span style={{color:C.textGhost,fontSize:11,marginLeft:3,fontFamily:FONT}}>/USD</span>
            </td>
            <td style={{padding:"10px 14px",borderBottom:`1px solid ${C.border}`}}>
              <div style={{display:"flex",alignItems:"center",gap:8,fontSize:12}}>
                <span style={{display:"flex",alignItems:"center",gap:4}}>
                  <span style={{width:5,height:5,borderRadius:1,background:C.red,display:"inline-block"}}/>
                  <span style={{color:C.textMid,fontFamily:FONT}}>{o.shortN}</span>
                </span>
                <span style={{color:C.textGhost,fontSize:10}}>→</span>
                <span style={{display:"flex",alignItems:"center",gap:4}}>
                  <span style={{width:5,height:5,borderRadius:1,background:C.green,display:"inline-block"}}/>
                  <span style={{color:C.textMid,fontFamily:FONT}}>{o.longN}</span>
                </span>
              </div>
            </td>
            <td style={{padding:"10px 14px",textAlign:"right",borderBottom:`1px solid ${C.border}`}}>
              <Mono style={{color:C.green,fontWeight:600,fontSize:13}}>+{(o.spread*100).toFixed(2)}%</Mono>
            </td>
            <td style={{padding:"10px 14px",textAlign:"center",borderBottom:`1px solid ${C.border}`}}>
              <div style={{display:"flex",justifyContent:"center"}}>
                <MiniSpark data={o.spark} color={C.indigo} w={52} h={16}/>
              </div>
            </td>
            <td style={{padding:"10px 14px",textAlign:"right",borderBottom:`1px solid ${C.border}`}}>
              <Mono style={{color:o.net>0?C.green:C.red,fontSize:12}}>
                {o.net>0?"+":""}{(o.net*100).toFixed(2)}%
              </Mono>
            </td>
            <td style={{padding:"10px 14px",textAlign:"right",borderBottom:`1px solid ${C.border}`}}>
              <div style={{display:"flex",alignItems:"center",justifyContent:"flex-end",gap:6}}>
                <div style={{width:36,height:3,borderRadius:2,background:"rgba(255,255,255,0.03)",overflow:"hidden"}}>
                  <div style={{width:`${o.conf*100}%`,height:"100%",borderRadius:2,background:C.indigo,
                    boxShadow:`0 0 4px ${C.indigoDim}`}}/>
                </div>
                <Mono style={{color:C.textMid,fontSize:11}}>{(o.conf*100).toFixed(0)}</Mono>
              </div>
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
};

/* ═══════════════════════════════════════════════════════════════
   MAIN DASHBOARD
   ═══════════════════════════════════════════════════════════════ */

export default function AegisDashboard(){
  const [vd,setVd]=useState(()=>genVenues());
  const [port]=useState(()=>genPortfolio());
  const [opps,setOpps]=useState(()=>genOpps(vd));
  const [logs,setLogs]=useState(()=>genLogs(vd,opps));
  const [now,setNow]=useState(new Date());
  const [tick,setTick]=useState(0);

  useEffect(()=>{
    const iv=setInterval(()=>{
      const nv=genVenues();setVd(nv);
      const no=genOpps(nv);setOpps(no);
      setLogs(genLogs(nv,no));setNow(new Date());setTick(t=>t+1);
    },7000);return()=>clearInterval(iv);
  },[]);

  const latest=port[port.length-1];
  const prev=port[port.length-2];
  const navChg=((latest.nav-prev.nav)/prev.nav)*100;
  const totalF=port.reduce((s,p)=>s+p.f,0);
  const healthy=vd.filter(v=>v.status==="up").length;

  const risks=[
    {label:"Drawdown",value:1.8,max:5,color:C.cyan},
    {label:"Venue Exp",value:22.1,max:25,color:C.cyan},
    {label:"Chain Conc",value:28.5,max:40,color:C.cyan},
    {label:"Margin Util",value:34.2,max:60,color:C.cyan},
    {label:"Oracle Div",value:0.12,max:0.5,color:C.cyan},
    {label:"Bridge Trn",value:2.1,max:10,color:C.cyan},
  ];

  return(
    <div style={{minHeight:"100vh",background:C.void,color:C.text,fontFamily:FONT,position:"relative",overflow:"hidden"}}>
      <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet"/>
      <style>{`
        *{box-sizing:border-box;margin:0;padding:0;}
        ::-webkit-scrollbar{width:4px;height:4px;}
        ::-webkit-scrollbar-track{background:transparent;}
        ::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.06);border-radius:4px;}
        @keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
        @keyframes pulse{0%,100%{opacity:0.5}50%{opacity:1}}
      `}</style>

      {/* Atmospheric background glows */}
      <Glow color={C.cyan} size={600} opacity={0.03} style={{top:-200,left:"30%"}}/>
      <Glow color={C.indigo} size={500} opacity={0.025} style={{bottom:-100,right:"10%"}}/>
      <Glow color={C.amber} size={300} opacity={0.02} style={{top:"40%",left:-100}}/>

      {/* Top bar */}
      <div style={{padding:"14px 28px",borderBottom:`1px solid ${C.border}`,display:"flex",
        alignItems:"center",justifyContent:"space-between",position:"relative",zIndex:10,
        background:"rgba(6,6,9,0.7)",backdropFilter:"blur(16px)"}}>
        <div style={{display:"flex",alignItems:"center",gap:14}}>
          <div style={{width:30,height:30,borderRadius:8,display:"flex",alignItems:"center",
            justifyContent:"center",background:`linear-gradient(135deg,rgba(34,211,238,0.15),rgba(129,140,248,0.15))`,
            border:`1px solid rgba(34,211,238,0.15)`}}>
            <Shield size={14} color={C.cyan} strokeWidth={2.5}/>
          </div>
          <span style={{fontSize:16,fontWeight:600,color:"#fafafa",letterSpacing:"-0.02em"}}>Aegis Protocol</span>
          <span style={{fontSize:10,color:C.textGhost,padding:"2px 8px",background:"rgba(255,255,255,0.02)",
            borderRadius:4,fontFamily:MONO}}>v1.0</span>
          <div style={{height:16,width:1,background:C.border,margin:"0 4px"}}/>
          <span style={{fontSize:10,fontWeight:600,letterSpacing:"0.07em",textTransform:"uppercase",
            padding:"3px 10px",borderRadius:5,display:"inline-flex",alignItems:"center",gap:5,
            background:"rgba(34,211,238,0.06)",color:C.cyan,border:`1px solid rgba(34,211,238,0.12)`}}>
            <Radio size={8} style={{animation:"pulse 2s ease infinite"}}/> OBSERVE ONLY
          </span>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:20}}>
          <div style={{display:"flex",alignItems:"center",gap:6}}>
            <Activity size={12} color={C.green}/>
            <Mono style={{color:C.textMid,fontSize:11}}>{healthy}/{vd.length} venues</Mono>
          </div>
          <Mono style={{color:C.textGhost,fontSize:11}}>
            {now.toLocaleTimeString("en-US",{hour12:false})} UTC
          </Mono>
        </div>
      </div>

      {/* Main content */}
      <div style={{padding:"22px 28px",position:"relative",zIndex:5,
        display:"flex",flexDirection:"column",gap:22}}>

        {/* Row 1: KPIs + Orbital Map */}
        <div style={{display:"grid",gridTemplateColumns:"1fr 320px",gap:22,animation:"fadeUp 0.5s ease"}}>

          {/* Left: KPIs stacked */}
          <div style={{display:"flex",flexDirection:"column",gap:2}}>
            {/* KPI cards row */}
            <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:2,borderRadius:14,overflow:"hidden"}}>
              {[
                {label:"PORTFOLIO NAV",value:USD(latest.nav),chg:navChg,
                  spark:port.slice(-24).map((p,i)=>({t:i,v:p.nav})),sparkColor:C.amber},
                {label:"FUNDING COLLECTED",value:USD(totalF),sub:"72h rolling"},
                {label:"BEST SPREAD",
                  value:`${(opps[0]?.spread*100||0).toFixed(1)}%`,sub:opps[0]?`${opps[0].shortN} → ${opps[0].longN}`:"—",
                  sparkColor:C.indigo},
                {label:"OPPORTUNITIES",value:`${opps.length}`,sub:"above threshold",sparkColor:C.cyan},
              ].map((kpi,i)=>(
                <div key={i} style={{padding:"18px 20px",background:C.panel,position:"relative",overflow:"hidden"}}>
                  {i===0&&<Glow color={C.amber} size={100} opacity={0.04} style={{top:-30,right:-20}}/>}
                  <div style={{fontSize:9,color:C.textDim,fontWeight:500,textTransform:"uppercase",
                    letterSpacing:"0.09em",marginBottom:10}}>{kpi.label}</div>
                  <div style={{display:"flex",alignItems:"flex-end",justifyContent:"space-between"}}>
                    <div>
                      <span style={{fontSize:22,fontWeight:600,color:"#fafafa"}}>{kpi.value}</span>
                      {kpi.chg!=null&&<span style={{marginLeft:8}}><Pct v={kpi.chg}/></span>}
                      {kpi.sub&&<div style={{fontSize:10,color:C.textGhost,marginTop:3}}>{kpi.sub}</div>}
                    </div>
                    {kpi.spark&&<MiniSpark data={kpi.spark} color={kpi.sparkColor||C.cyan} w={72} h={28}/>}
                  </div>
                </div>
              ))}
            </div>

            {/* NAV Chart */}
            <GlassCard style={{padding:"16px 18px 8px",flex:1,minHeight:180}} glow glowColor={C.amber}>
              <div style={{fontSize:10,color:C.textDim,fontWeight:500,textTransform:"uppercase",
                letterSpacing:"0.08em",marginBottom:10}}>PERFORMANCE — 72H</div>
              <ResponsiveContainer width="100%" height={150}>
                <AreaChart data={port} margin={{top:4,right:4,bottom:0,left:4}}>
                  <defs>
                    <linearGradient id="ng" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={C.amber} stopOpacity={0.15}/>
                      <stop offset="100%" stopColor={C.amber} stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="t" hide/><YAxis hide domain={["dataMin-500","dataMax+500"]}/>
                  <Tooltip contentStyle={{background:C.surface,border:`1px solid ${C.borderHi}`,borderRadius:8,
                    fontSize:11,fontFamily:MONO}} itemStyle={{color:C.amber}}
                    formatter={v=>[`$${v.toLocaleString()}`,"NAV"]} labelFormatter={()=>""}/>
                  <Area type="monotone" dataKey="nav" stroke={C.amber} strokeWidth={2} fill="url(#ng)" dot={false}/>
                </AreaChart>
              </ResponsiveContainer>
            </GlassCard>
          </div>

          {/* Right: Orbital Map + Risk Rings */}
          <div style={{display:"flex",flexDirection:"column",gap:2}}>
            <GlassCard style={{height:260,padding:0}} glow glowColor={C.cyan}>
              <OrbitalMap venues={vd} opps={opps}/>
            </GlassCard>
            <GlassCard style={{padding:"16px 14px"}}>
              <div style={{fontSize:9,color:C.textDim,fontWeight:500,textTransform:"uppercase",
                letterSpacing:"0.08em",marginBottom:14,textAlign:"center"}}>RISK LIMITS</div>
              <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:10}}>
                {risks.map(r=><RingGauge key={r.label} value={r.value} max={r.max}
                  label={r.label} color={r.color} size={64}/>)}
              </div>
            </GlassCard>
          </div>
        </div>

        {/* Row 2: Heatmap + Terminal */}
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:22,animation:"fadeUp 0.6s ease"}}>
          <GlassCard style={{padding:"16px 18px"}} glow glowColor={C.green}>
            <div style={{fontSize:10,color:C.textDim,fontWeight:500,textTransform:"uppercase",
              letterSpacing:"0.08em",marginBottom:12}}>FUNDING RATE MATRIX — ANNUALIZED</div>
            <Heatmap venues={vd}/>
          </GlassCard>
          <GlassCard glow glowColor={C.indigo}>
            <div style={{padding:"14px 16px 0",fontSize:10,color:C.textDim,fontWeight:500,
              textTransform:"uppercase",letterSpacing:"0.08em",display:"flex",
              alignItems:"center",justifyContent:"space-between"}}>
              <span>AGENT LOG</span>
              <span style={{fontSize:9,color:C.textGhost,fontFamily:MONO,
                animation:"pulse 3s ease infinite"}}>● live</span>
            </div>
            <TerminalFeed logs={logs}/>
          </GlassCard>
        </div>

        {/* Row 3: Opportunities */}
        <div style={{animation:"fadeUp 0.7s ease"}}>
          <GlassCard style={{padding:"18px 20px"}} glow glowColor={C.cyan}>
            <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",marginBottom:14}}>
              <div style={{fontSize:10,color:C.textDim,fontWeight:500,textTransform:"uppercase",
                letterSpacing:"0.08em"}}>ARBITRAGE OPPORTUNITIES</div>
              <div style={{display:"flex",alignItems:"center",gap:8}}>
                <Zap size={12} color={C.cyan}/>
                <Mono style={{color:C.cyan,fontSize:11}}>{opps.length} active</Mono>
              </div>
            </div>
            <OppsTable opps={opps}/>
          </GlassCard>
        </div>

        {/* Footer */}
        <div style={{textAlign:"center",padding:"16px 0 8px"}}>
          <span style={{fontSize:10,color:C.textGhost,letterSpacing:"0.1em",textTransform:"uppercase",fontFamily:MONO}}>
            Aegis Protocol — 9 venues · 7 chains · {tick>0?"live":"initializing"}
          </span>
        </div>
      </div>
    </div>
  );
}
