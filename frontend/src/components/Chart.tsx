import {useState} from 'react';
import {compact, day, rub} from '../api';
import type {Point} from '../types';

export function Chart({base,alternative}:{base:Point[];alternative?:Point[]}) {
  const [active,setActive]=useState<number|null>(null);
  if(!base.length)return null;
  const values=[0,...base.map(p=>p.balance_rub),...(alternative||[]).map(p=>p.balance_rub)];
  const low=Math.min(...values), high=Math.max(...values),span=Math.max(1000,high-low),lo=low-span*.14,hi=high+span*.12;
  const x=(i:number)=>70+i/Math.max(1,base.length-1)*900,y=(n:number)=>275-(n-lo)/(hi-lo)*245;
  const path=(points:Point[])=>points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(p.balance_rub).toFixed(2)}`).join(' ');
  const d=path(base),a=active===null?null:base[active];
  return <div className="chart"><div className="chart-legend"><span><i className="legend-line"/>Текущий план</span>{alternative&&<span><i className="legend-line teal"/>Сценарий</span>}</div><div className="chart-canvas" onMouseLeave={()=>setActive(null)}><svg viewBox="0 0 1000 315" role="img" aria-label="График ежедневного остатка: синий — текущий план, бирюзовый — сценарий" onMouseMove={e=>{const r=e.currentTarget.getBoundingClientRect();setActive(Math.max(0,Math.min(base.length-1,Math.round(((e.clientX-r.left)/r.width*1000-70)/900*(base.length-1)))));}}>
    <defs><linearGradient id="cash-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#2165e8" stopOpacity=".14"/><stop offset="1" stopColor="#2165e8" stopOpacity=".005"/></linearGradient></defs>
    {[0,1,2,3,4].map(j=>{const v=lo+(hi-lo)*j/4;return <g key={j}><line x1="70" x2="970" y1={y(v)} y2={y(v)} stroke="#e8edf5" strokeDasharray="4 5"/><text x="55" y={y(v)+4} textAnchor="end">{compact(v)}</text></g>;})}
    <path d={d+` L970,275 L70,275 Z`} fill="url(#cash-area)"/>
    <line x1="70" x2="970" y1={y(0)} y2={y(0)} stroke="#ed9da7" strokeDasharray="6 5"/>
    <path d={d} fill="none" stroke="#2463e9" strokeWidth="2.8" strokeLinejoin="round"/>
    {alternative&&<path d={path(alternative)} fill="none" stroke="#0ca58b" strokeWidth="2.8" strokeLinejoin="round"/>}
    {[0,1,2,3,4,5,6].map(j=>{const i=Math.round((base.length-1)*j/6);return <text key={j} x={x(i)} y="305" textAnchor="middle">{i===0?'Сегодня':`+${i} д.`}</text>;})}
    {active!==null&&<g><line x1={x(active)} x2={x(active)} y1="25" y2="275" stroke="#93a9c7" strokeDasharray="3 4"/><circle cx={x(active)} cy={y(base[active].balance_rub)} r="4" fill="#2463e9"/>{alternative&&<circle cx={x(active)} cy={y(alternative[active].balance_rub)} r="4" fill="#0ca58b"/>}</g>}
  </svg>{a&&<div className="chart-tooltip"><strong>{day(a.date)}</strong><span>Текущий план: {rub(a.balance_rub)}</span>{alternative&&<span>Сценарий: {rub(alternative[active!].balance_rub)}</span>}</div>}</div><label className="sr-only">День графика<input type="range" min="0" max={base.length-1} value={active??0} onChange={e=>setActive(Number(e.target.value))}/></label></div>;
}
