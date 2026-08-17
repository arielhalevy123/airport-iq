"""The browser interface.

One design decision drives the whole layout: **the agent's tool calls are shown to the user.**

Most chat UIs hide the machinery and present a paragraph of prose, which is precisely the shape
that makes an analyst distrust an AI answer — they cannot see where a number came from. Here
every answer displays which tools ran, in what order, and the assumptions the system made. That
turns "trust me" into "check me", and it is the single most useful thing this interface does.

Plain HTML/CSS/JS with no build step and no CDN, so it runs from `python -m airportiq.api.server`
with nothing installed. A React app would need npm install and a build before a reviewer could
see anything, which is a worse trade for a one-day deliverable.

VOICE
-----
The brief lists voice as a bonus, and it is done with the browser's own Web Speech API —
SpeechRecognition in, speechSynthesis out. No key, no cloud STT vendor, no dependency, and it
degrades to a hidden button when the browser lacks support rather than breaking the page.

One deliberate asymmetry: voice INPUT is the whole question, but voice OUTPUT is only the prose
answer. The tool trace and the assumptions list stay on screen and are never spoken. Reading a
list of caveats aloud is how a listener stops hearing them, and the caveats are the part of this
system that must not be lost. Speech is for the finding; the screen is for the evidence.

THE LOOK, AND WHY IT IS NOT A GENERIC CHAT BOX
----------------------------------------------
The visual language is borrowed from aviation instrumentation rather than from consumer chat:
IATA codes set as boxed tags, all figures in monospace so columns of percentiles line up, and a
dark palette reading as a night-ops console with amber reserved exclusively for constraint
flags. Amber appears nowhere decorative — if something is amber, an airport is legally or
physically capped.

The scorecard panel is the substantive half of this. Every answer that touches an airport is
accompanied by that airport's percentile bars, drawn straight from the deterministic engine.
The prose sits above the numbers that produced it, so a reader can check one against the other
in the same glance. Crucially the panel is built from the TOOL-CALL ARGUMENTS, not by parsing
the model's sentence for airport codes — a panel derived from the prose would agree with the
prose by construction, and would verify nothing.
"""

INDEX = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirportIQ — US airport capacity investment</title>
<style>
 /* Night-ops console. Amber is reserved for constraint flags and used nowhere
    decorative — if something is amber, an airport is capped. */
 :root{
   --bg:#0d1117; --panel:#161c24; --panel2:#1b232d; --ink:#e6edf3; --muted:#7d8896;
   --line:#232c38; --accent:#4db8d4; --accent-dim:#2a6b7d; --warn:#e0a44c; --ok:#5ec27e;
   --grid:rgba(77,184,212,.05);
 }
 @media (prefers-color-scheme: light){
   :root{ --bg:#f4f6f8; --panel:#fff; --panel2:#f0f3f6; --ink:#10161d; --muted:#5b6672;
          --line:#dde3ea; --accent:#0f6b86; --accent-dim:#8fc4d4; --warn:#9a5f10;
          --ok:#1c7a45; --grid:rgba(15,107,134,.05); }
 }
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:15px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
      background-image:
        linear-gradient(var(--grid) 1px,transparent 1px),
        linear-gradient(90deg,var(--grid) 1px,transparent 1px);
      background-size:44px 44px}
 .wrap{max-width:860px;margin:0 auto;padding:30px 20px 132px}

 /* header: a runway threshold, not a logo */
 header{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:8px}
 .brand{display:flex;align-items:baseline;gap:11px}
 h1{font-size:18px;margin:0;letter-spacing:.06em;text-transform:uppercase;font-weight:650}
 h1 span{color:var(--accent)}
 .rwy{flex:1;height:9px;position:relative;overflow:hidden;
      border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
 .rwy:after{content:"";position:absolute;top:50%;left:0;right:0;height:1px;
            transform:translateY(-50%);
            background:repeating-linear-gradient(90deg,
              var(--accent-dim) 0 14px,transparent 14px 28px)}
 .meta{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
       letter-spacing:.08em}
 .tag{color:var(--muted);font-size:13.5px;margin:10px 0 0}
 .tag b{color:var(--ink);font-weight:600}

 .examples{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 4px}
 .ex{background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--accent-dim);
     border-radius:3px;padding:7px 13px;font-size:13px;cursor:pointer;color:var(--muted);
     transition:.12s}
 .ex:hover{border-left-color:var(--accent);color:var(--ink);background:var(--panel2)}

 .msg{margin:18px 0}
 .you{text-align:right}
 .you span{display:inline-block;background:var(--accent-dim);color:var(--ink);
           padding:9px 14px;border-radius:3px;max-width:80%;text-align:left;
           border-right:2px solid var(--accent)}
 .bot{background:var(--panel);border:1px solid var(--line);
      border-left:2px solid var(--accent);border-radius:3px;padding:16px 18px}
 .bot p{margin:0 0 10px;white-space:pre-wrap}
 .bot p:last-child{margin:0}
 /* IATA codes read as instrument labels, not prose */
 .bot p code,.iata{font:600 12.5px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
       letter-spacing:.06em;background:var(--panel2);border:1px solid var(--line);
       border-radius:2px;padding:2px 5px;color:var(--accent)}

 details{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
 summary{cursor:pointer;color:var(--muted);font-size:12.5px;
         list-style:none;user-select:none}
 summary::-webkit-details-marker{display:none}
 summary:before{content:"▸ ";color:var(--accent)}
 details[open] summary:before{content:"▾ "}
 .trace{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
        margin-top:8px}
 .trace div{padding:3px 0;border-left:1px solid var(--line);padding-left:9px}
 .trace b{color:var(--accent);font-weight:600}
 .assume{font-size:12.5px;color:var(--muted);margin-top:8px}
 .assume li{margin:4px 0}

 .badge{display:inline-block;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;
        padding:2px 8px;border-radius:2px;letter-spacing:.05em;
        border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:middle}
 .badge.warn{color:var(--warn);border-color:var(--warn)}

 /* ---- scorecard: the engine's numbers, beside the prose that used them ---- */
 .cards{display:grid;gap:10px;margin-top:14px;
        grid-template-columns:repeat(auto-fit,minmax(232px,1fr))}
 .card{background:var(--panel2);border:1px solid var(--line);border-radius:3px;padding:11px 13px}
 .card.capped{border-color:var(--warn)}
 .chead{display:flex;align-items:center;gap:8px;margin-bottom:9px}
 .chead .nm{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
            white-space:nowrap;flex:1}
 .chead .rk{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
 .kpi{display:grid;grid-template-columns:1fr 34px;gap:7px;align-items:center;
      font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
      margin:5px 0}
 .kpi .lbl{grid-column:1/-1;letter-spacing:.03em}
 .bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden}
 .bar i{display:block;height:100%;background:var(--accent)}
 .bar.hi i{background:var(--warn)}
 .val{text-align:right;color:var(--ink)}
 .cflag{margin-top:9px;font-size:11.5px;color:var(--warn);line-height:1.45}
 .cmiss{margin-top:7px;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}

 /* ---- stat explainers: every number carries an ⓘ that shows what the stat is and
    how THIS airport's figure was derived, straight from the pure explain layer ---- */
 .info{width:15px;height:15px;border-radius:50%;border:1px solid var(--line);flex:none;
       background:none;color:var(--muted);cursor:help;padding:0;margin-left:5px;
       font:600 10px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
       display:inline-flex;align-items:center;justify-content:center;vertical-align:1px;
       text-transform:none;letter-spacing:0}
 .info:hover,.info:focus{color:var(--accent);border-color:var(--accent);outline:none}
 .tip{position:fixed;z-index:50;max-width:340px;background:var(--panel);
      border:1px solid var(--accent-dim);border-radius:3px;padding:11px 13px;
      font-size:12px;line-height:1.5;color:var(--ink);
      box-shadow:0 6px 24px rgba(0,0,0,.45)}
 .tip[hidden]{display:none}
 .tip h4{margin:0 0 6px;font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace;
         letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
 .tip .twhat{margin-bottom:8px}
 .tip .thl{font:600 10px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
           letter-spacing:.08em;text-transform:uppercase;margin-bottom:3px}
 .tip .thow div{font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
                padding:1px 0}
 .tip .tcave{margin-top:8px;color:var(--warn);font-size:11.5px;
             border-top:1px solid var(--line);padding-top:7px}
 .tip .thint{margin-top:8px;color:var(--muted);font:10.5px ui-monospace,SFMono-Regular,
             Menlo,monospace;letter-spacing:.05em;text-transform:uppercase}

 /* the click-through window: where the data came from, what it was, and the calculation,
    in plain words first. Hover teases the meaning; the window carries the whole story. */
 .ovl{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:60;display:flex;
      align-items:center;justify-content:center;padding:20px}
 .modal{background:var(--panel);border:1px solid var(--accent-dim);border-radius:4px;
        max-width:500px;width:100%;max-height:82vh;overflow:auto;padding:18px 20px 20px;
        position:relative;box-shadow:0 14px 48px rgba(0,0,0,.5)}
 .modal h3{margin:0;font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;
           letter-spacing:.06em;text-transform:uppercase;color:var(--accent);
           padding-right:28px}
 .modal .mcode{color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace;
               margin:2px 0 4px}
 .modal .msec{margin-top:13px}
 .modal .mh{font:600 10px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
            letter-spacing:.09em;text-transform:uppercase;margin-bottom:4px}
 .modal .msimple{font-size:13.5px;line-height:1.6}
 .modal .msrc{font-size:12.5px;color:var(--muted)}
 .modal .mhow div{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
                  padding:2px 0}
 .modal .mcave{color:var(--warn);font-size:12px;line-height:1.5}
 .modal .x{position:absolute;top:8px;right:10px;border:none;background:none;
           color:var(--muted);font-size:18px;line-height:1;cursor:pointer;padding:4px}
 .modal .x:hover{color:var(--ink)}
 .kpi .val{cursor:help}

 form{position:fixed;bottom:0;left:0;right:0;background:var(--bg);
      border-top:1px solid var(--line);padding:14px 20px}
 .row{max-width:860px;margin:0 auto;display:flex;gap:8px}
 input{flex:1;padding:11px 14px;font-size:15px;font-family:inherit;color:var(--ink);
       background:var(--panel);border:1px solid var(--line);border-radius:3px}
 input:focus{outline:none;border-color:var(--accent)}
 button{padding:11px 20px;font:600 13px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
        letter-spacing:.09em;text-transform:uppercase;border:1px solid var(--accent);
        border-radius:3px;background:var(--accent);color:var(--bg);cursor:pointer}
 button:disabled{opacity:.45;cursor:default}
 .thinking{color:var(--muted);font-style:italic}

 /* live tool trace, streamed */
 .live{margin-bottom:2px}
 .live:empty{display:none}
 .step{display:flex;align-items:center;gap:9px;padding:5px 0;
       font:13.5px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
       animation:slide .18s ease-out}
 @keyframes slide{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}
 .dot{width:5px;height:5px;border-radius:50%;background:var(--line);flex:none}
 .step.run .dot{background:var(--accent);animation:pulse 1s ease-in-out infinite}
 .step.run{color:var(--accent)}
 .step.ok .dot{background:var(--ok)}
 .cursor{display:inline-block;width:7px;height:14px;vertical-align:-2px;margin-left:2px;
         background:var(--accent);animation:blink .9s step-end infinite}
 @keyframes blink{50%{opacity:0}}

 /* voice */
 .icon{padding:11px 13px;background:var(--panel);color:var(--muted);
       border:1px solid var(--line);border-radius:3px;font-size:15px;line-height:1}
 .icon:hover{color:var(--ink);border-color:var(--accent)}
 .icon[hidden]{display:none}
 .icon.on{background:var(--accent);color:#fff;border-color:var(--accent)}
 .icon.rec{background:var(--warn);color:#fff;border-color:var(--warn);
           animation:pulse 1.2s ease-in-out infinite}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
 .heard{font-size:12.5px;color:var(--muted);margin:6px 0 0;text-align:center}
</style></head><body>
<div class="wrap">
 <header>
  <div class="brand">
   <h1>Airport<span>IQ</span></h1>
   <div class="rwy"></div>
   <div class="meta">CAPACITY &middot; US</div>
  </div>
  <p class="tag">US airport capacity investment analysis.
     Every figure is computed by a <b>deterministic scoring engine</b> — the model chooses
     which questions to ask and writes the prose, never the numbers.</p>
 </header>

 <div class="examples" id="ex">
  <div class="ex">Which airports in New England are strong candidates for terminal expansion?</div>
  <div class="ex">Compare LA and Santa Ana congestion levels</div>
  <div class="ex">What percentage of flights out of Anchorage are long haul?</div>
  <div class="ex">What is the unmet flight demand at SFO and why?</div>
  <div class="ex">What will a new terminal at SFO cost?</div>
 </div>

 <div id="log"></div>
</div>

<form onsubmit="ask(event)">
 <div class="row">
  <button type="button" class="icon" id="mic" hidden title="Ask by voice">&#127908;</button>
  <input id="q" placeholder="Ask about US airport capacity…" autocomplete="off" autofocus>
  <button type="button" class="icon" id="spk" hidden title="Read answers aloud">&#128264;</button>
  <button id="go">Ask</button>
 </div>
 <p class="heard" id="heard"></p>
</form>

<script>
const SID = Math.random().toString(36).slice(2);
const log = document.getElementById('log');
const q = document.getElementById('q');
const go = document.getElementById('go');

document.querySelectorAll('.ex').forEach(e =>
  e.onclick = () => { q.value = e.textContent.trim(); ask(); });

function el(tag, cls, text){
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text) d.textContent = text;
  return d;
}

const KPI_LABEL = {
  delay_congestion:'delay congestion', peak_pressure:'peak pressure',
  gate_saturation:'gate saturation', airside_saturation:'airside saturation',
  airside_headroom:'airside headroom', demand_growth:'demand growth',
  international_intensity:'intl intensity'
};

/* ---- stat explainers ----------------------------------------------------------
   Two levels, both fed by the `explain` payload the server derives with the same pure
   layer that computed the score. HOVER answers "what does this stat mean". CLICK opens
   a window with the whole story: plain words first, then where the data came from, then
   the calculation step by step, then the fine print. All content goes in via
   textContent, so it is as XSS-safe as the rest of the page. */
const tip = el('div','tip'); tip.hidden = true; document.body.appendChild(tip);

function showTip(anchor, title, d){
  tip.textContent = '';
  tip.appendChild(el('h4',null,title));
  if (d.what) tip.appendChild(el('div','twhat',d.what));
  tip.appendChild(el('div','thint','click for the full story — data source and calculation'));
  tip.hidden = false;
  const r = anchor.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  let y = r.bottom + 8;
  if (y + h > window.innerHeight - 8) y = r.top - h - 8;   // flip above near the bottom
  tip.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 12)) + 'px';
  tip.style.top = Math.max(8, y) + 'px';
}

function hideTip(){ tip.hidden = true; }

function openStory(title, code, d){
  hideTip();
  const ovl = el('div','ovl');
  const box = el('div','modal');
  const x = el('button','x','×'); x.type = 'button';
  x.setAttribute('aria-label','close');
  box.appendChild(x);
  box.appendChild(el('h3',null,title));
  if (code) box.appendChild(el('div','mcode',code));

  const sec = (label, cls, fill) => {
    const s = el('div','msec');
    s.appendChild(el('div','mh',label));
    const body = el('div',cls);
    fill(body);
    s.appendChild(body);
    box.appendChild(s);
  };
  if (d.simple) sec('in plain words','msimple', b => b.textContent = d.simple);
  if (d.source) sec('where the data comes from','msrc', b => b.textContent = d.source);
  if (d.how && d.how.length)
    sec('the calculation, step by step','mhow',
        b => d.how.forEach(l => b.appendChild(el('div',null,l))));
  if (d.caveat) sec('the fine print','mcave', b => b.textContent = d.caveat);

  const close = () => { ovl.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = ev => { if (ev.key === 'Escape') close(); };
  x.onclick = close;
  ovl.onclick = ev => { if (ev.target === ovl) close(); };
  document.addEventListener('keydown', onKey);
  ovl.appendChild(box);
  document.body.appendChild(ovl);
}

function wireStat(elm, title, code, d){
  elm.onmouseenter = () => showTip(elm, title, d);
  elm.onmouseleave = hideTip;
  elm.onfocus = () => showTip(elm, title, d);
  elm.onblur = hideTip;
  elm.onclick = ev => { ev.stopPropagation(); openStory(title, code, d); };
}

function info(title, code, d){
  const b = el('button','info','i');
  b.type = 'button';
  b.setAttribute('aria-label', 'what is ' + title + ' and how was it computed?');
  wireStat(b, title, code, d);
  return b;
}

window.addEventListener('scroll', hideTip, {passive:true});

function scorecards(list){
  const grid = el('div','cards');
  list.forEach(c => {
    const capped = (c.flags||[]).length > 0;
    const card = el('div','card' + (capped ? ' capped' : ''));

    const ex = c.explain || {};
    const who = c.code + (c.name ? ' — ' + c.name : '');

    const head = el('div','chead');
    head.appendChild(el('span','iata',c.code));
    head.appendChild(el('span','nm',c.name || ''));
    if (c.rank){
      head.appendChild(el('span','rk','#'+c.rank+' '+(c.hub_class||'')));
      if (ex.rank) head.appendChild(info('rank', who, ex.rank));
    }
    if (typeof c.composite === 'number'){
      head.appendChild(el('span','rk','score '+c.composite.toFixed(1)));
      if (ex.composite) head.appendChild(info('composite score', who, ex.composite));
    }
    card.appendChild(head);

    // Percentiles are within the airport's own hub class — the same caveat the tools
    // return. Bars are therefore comparable down a column, never across hub classes.
    Object.entries(c.kpis||{}).forEach(([k,v]) => {
      if (typeof v !== 'number') return;
      const row = el('div','kpi');
      const title = KPI_LABEL[k] || k.replace(/_/g,' ');
      const lbl = el('div','lbl', title);
      if (ex.kpis && ex.kpis[k]) lbl.appendChild(info(title, who, ex.kpis[k]));
      row.appendChild(lbl);
      const bar = el('div','bar' + (v >= 80 ? ' hi' : ''));
      const fill = el('i'); fill.style.width = Math.max(0,Math.min(100,v)) + '%';
      bar.appendChild(fill);
      row.appendChild(bar);
      const val = el('div','val', v.toFixed(0));
      // "hover on the number": the value itself explains too, not only the ⓘ
      if (ex.kpis && ex.kpis[k]) wireStat(val, title, who, ex.kpis[k]);
      row.appendChild(val);
      card.appendChild(row);
    });

    (c.flags||[]).forEach(f => card.appendChild(el('div','cflag','⚠ ' + f)));
    if ((c.missing||[]).length)
      card.appendChild(el('div','cmiss','computed without: ' + c.missing.join(', ')));
    grid.appendChild(card);
  });
  return grid;
}

/* Streamed turn. The interesting latency here is the TOOL CALLS, not token generation:
   the model thinks, queries, thinks again. A spinner hides precisely the part worth
   watching, so each tool call appears live as the agent decides to make it. The wait
   becomes the demonstration. */
function ask(ev){
  if (ev) ev.preventDefault();
  const text = q.value.trim();
  if (!text) return;

  const you = el('div','msg you'); you.appendChild(el('span',null,text));
  log.appendChild(you);
  q.value = ''; go.disabled = true;

  const wrap = el('div','msg');
  const bot = el('div','bot');
  const live = el('div','live');            // tool calls, as they happen
  const para = el('p');                     // the answer, as it streams
  para.appendChild(el('span','cursor',''));
  bot.appendChild(live); bot.appendChild(para);
  wrap.appendChild(bot); log.appendChild(wrap);
  const stick = () => window.scrollTo(0, document.body.scrollHeight);
  stick();

  const step = (label, cls) => {
    const row = el('div','step ' + (cls||''));
    row.appendChild(el('span','dot',''));
    row.appendChild(el('span',null,label));
    live.appendChild(row); stick();
    return row;
  };
  const thinking = step('reasoning…','run');

  let answer = '', done = null;

  fetch('/v1/chat/stream', {method:'POST', headers:{'content-type':'application/json'},
                            body: JSON.stringify({question:text, session_id:SID})})
    .then(resp => {
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';

      // Minimal SSE parser. EventSource cannot be used because it is GET-only and the
      // question travels in a POST body.
      const pump = () => reader.read().then(({done:fin, value}) => {
        if (fin) return;
        buf += dec.decode(value, {stream:true});
        let i;
        while ((i = buf.indexOf('\\n\\n')) !== -1){
          const frame = buf.slice(0, i); buf = buf.slice(i + 2);
          const ev = (frame.match(/^event: (.*)$/m) || [])[1];
          const dm = frame.match(/^data: (.*)$/m);
          if (!ev || !dm) continue;
          // Never swallow a frame silently: a dropped 'done' loses the trace and the
          // scorecards while the answer still renders, which looks like a UI that simply
          // does not have those features rather than one that failed.
          let d;
          try { d = JSON.parse(dm[1]); }
          catch(err){ console.error('[airportiq] bad SSE frame', ev, err, dm[1]); continue; }
          handle(ev, d);
        }
        return pump();
      });

      const handle = (ev, d) => {
        if (ev === 'tool_call'){
          // A tool call means the deltas seen so far were mid-plan reasoning, not the final
          // answer. Drop them so intermediate prose does not leak into the answer paragraph
          // once the real answer starts streaming after the tool call resolves.
          answer = '';
          para.textContent = '';
          thinking.remove();
          step(d.tool + ' ' + shortArgs(d.args), 'run');
        } else if (ev === 'tool_result'){
          const rows = live.querySelectorAll('.step.run');
          const last = rows[rows.length-1];
          if (last){ last.classList.remove('run'); last.classList.add('ok'); }
          if (d.cached) step(d.tool + '  (cached)','ok');
        } else if (ev === 'delta'){
          thinking.remove();
          answer += d;
          para.textContent = answer;
          para.appendChild(el('span','cursor',''));
          stick();
        } else if (ev === 'error'){
          thinking.remove();
          para.textContent = 'error: ' + d.error;
        } else if (ev === 'done'){
          done = d;
        }
      };

      return pump();
    })
    .then(() => {
      thinking.remove();
      const d = done || {};
      bot.removeChild(para);
      const finalText = (d.answer || answer || '(no answer)');
      finalText.split(/\\n\\n+/).forEach(par => bot.appendChild(el('p',null,par)));

      if (d.intent === 'unsupported')
        bot.querySelector('p').appendChild(el('span','badge warn','outside the data'));

      // The engine's own numbers, next to the sentence that used them. Built from the
      // tool-call arguments server-side, never from parsing the prose — see the module
      // docstring. A capped airport is outlined amber because a legal ceiling changes
      // the recommendation regardless of how good the other metrics look.
      if (d.scorecards && d.scorecards.length) bot.appendChild(scorecards(d.scorecards));

      // The point of this interface: show what the agent actually did.
      if (d.trace && d.trace.length){
        const det = el('details');
        det.appendChild(el('summary',null,
          `${d.trace.length} tool call${d.trace.length>1?'s':''} — show what was queried`));
        const t = el('div','trace');
        d.trace.forEach(c => {
          const row = el('div');
          row.appendChild(el('b',null,c.tool));
          row.appendChild(document.createTextNode(
            ' ' + (c.args||'') + (c.cached ? '   (cached)' : '')));
          t.appendChild(row);
        });
        det.appendChild(t); bot.appendChild(det);
      }

      if (d.assumptions && d.assumptions.length){
        const det = el('details'); det.open = true;
        det.appendChild(el('summary',null,'assumptions and caveats'));
        const ul = el('ul','assume');
        d.assumptions.forEach(a => ul.appendChild(el('li',null,a)));
        det.appendChild(ul); bot.appendChild(det);
      }

      // Speak the finding only. The trace and the caveats stay on screen — see the
      // module docstring for why they are deliberately never read aloud.
      if (speakOn && finalText) speak(finalText);
    })
    // para may already have been detached by the finaliser, so an error written only
    // there would be invisible. Log it and put it somewhere still on screen.
    .catch(e => {
      console.error('[airportiq] turn failed:', e);
      const p = el('p',null,'error: ' + (e && e.message ? e.message : e));
      bot.appendChild(p);
    })
    .finally(() => { go.disabled=false; q.focus(); stick(); });
}

function shortArgs(s){
  try {
    const o = JSON.parse(s || '{}');
    const v = o.airport || o.region || (o.airports||[]).join(', ') || o.profile || '';
    return v ? '· ' + v : '';
  } catch(e){ return ''; }
}

/* ---------------------------------------------------------------- voice ---
   Browser-native Web Speech API. Both legs are feature-detected and each button
   stays hidden if its half is unsupported, so Firefox (no SpeechRecognition) still
   gets working text-to-speech instead of a dead microphone. */

const mic = document.getElementById('mic');
const spk = document.getElementById('spk');
const heard = document.getElementById('heard');

/* --- output: read the finding aloud --- */
let speakOn = false;
const canSpeak = 'speechSynthesis' in window;
if (canSpeak){
  spk.hidden = false;
  spk.onclick = () => {
    speakOn = !speakOn;
    spk.classList.toggle('on', speakOn);
    spk.title = speakOn ? 'Stop reading answers aloud' : 'Read answers aloud';
    if (!speakOn) speechSynthesis.cancel();
  };
}

function speak(text){
  if (!canSpeak) return;
  speechSynthesis.cancel();
  // "NAS" is read as a word by the synthesiser and comes out as noise; "%" is skipped
  // entirely by some voices, which silently turns "the 87th percentile" into "the 87".
  // Fixed here rather than in the model's prose, because the written answer must stay
  // precise for the analyst reading it on screen.
  const spoken = text
    .replace(/\\bNAS\\b/g, 'N A S')
    .replace(/%/g, ' percent')
    .slice(0, 700);
  const u = new SpeechSynthesisUtterance(spoken);
  u.lang = 'en-US'; u.rate = 1.03;
  speechSynthesis.speak(u);
}

/* --- input: ask by voice --- */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR){
  mic.hidden = false;
  const rec = new SR();
  rec.lang = 'en-US';
  rec.interimResults = true;
  rec.continuous = false;
  let listening = false, finalText = '';

  mic.onclick = () => {
    if (listening){ rec.stop(); return; }
    finalText = ''; heard.textContent = 'listening…';
    speechSynthesis.cancel();          // never talk over the user
    try { rec.start(); } catch(e){ heard.textContent = 'could not start the microphone'; }
  };

  rec.onstart = () => { listening = true; mic.classList.add('rec'); };

  rec.onresult = (ev) => {
    let interim = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++){
      const r = ev.results[i];
      if (r.isFinal) finalText += r[0].transcript;
      else interim += r[0].transcript;
    }
    q.value = (finalText + interim).trim();
    heard.textContent = q.value ? 'heard: ' + q.value : 'listening…';
  };

  rec.onerror = (ev) => {
    heard.textContent = ev.error === 'not-allowed'
      ? 'microphone permission denied'
      : 'speech error: ' + ev.error;
  };

  // Submit on end rather than on the first final result: a question said with a pause
  // in the middle produces two final results, and asking after the first one cuts the
  // user off mid-sentence.
  rec.onend = () => {
    listening = false; mic.classList.remove('rec');
    const text = q.value.trim();
    if (text){ heard.textContent = ''; ask(); }
    else heard.textContent = '';
  };
}
</script></body></html>
"""
