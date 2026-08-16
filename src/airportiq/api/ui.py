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
"""

INDEX = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirportIQ — US airport capacity investment</title>
<style>
 :root{
   --bg:#fbfbfa; --panel:#fff; --ink:#1a1a1a; --muted:#6b6b6b; --line:#e5e4e1;
   --accent:#1f4e79; --warn:#b4530a; --ok:#1e6b3a;
 }
 @media (prefers-color-scheme: dark){
   :root{ --bg:#161615; --panel:#1e1e1d; --ink:#eceae4; --muted:#9a978f;
          --line:#33322f; --accent:#7fb2e0; --warn:#e0a060; --ok:#7dc79a; }
 }
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
 .wrap{max-width:820px;margin:0 auto;padding:32px 20px 120px}
 header{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:22px}
 h1{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}
 .tag{color:var(--muted);font-size:13.5px;margin:0}
 .tag b{color:var(--ink);font-weight:600}

 .examples{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 4px}
 .ex{background:var(--panel);border:1px solid var(--line);border-radius:16px;
     padding:6px 13px;font-size:13px;cursor:pointer;color:var(--muted);transition:.12s}
 .ex:hover{border-color:var(--accent);color:var(--ink)}

 .msg{margin:18px 0}
 .you{text-align:right}
 .you span{display:inline-block;background:var(--accent);color:#fff;
           padding:9px 14px;border-radius:14px 14px 3px 14px;max-width:80%;text-align:left}
 .bot{background:var(--panel);border:1px solid var(--line);border-radius:3px 14px 14px 14px;
      padding:16px 18px}
 .bot p{margin:0 0 10px;white-space:pre-wrap}
 .bot p:last-child{margin:0}

 details{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
 summary{cursor:pointer;color:var(--muted);font-size:12.5px;
         list-style:none;user-select:none}
 summary::-webkit-details-marker{display:none}
 summary:before{content:"▸ ";color:var(--accent)}
 details[open] summary:before{content:"▾ "}
 .trace{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
        margin-top:8px}
 .trace div{padding:3px 0}
 .trace b{color:var(--accent);font-weight:600}
 .assume{font-size:12.5px;color:var(--muted);margin-top:8px}
 .assume li{margin:4px 0}

 .badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;
        border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:middle}
 .badge.warn{color:var(--warn);border-color:var(--warn)}

 form{position:fixed;bottom:0;left:0;right:0;background:var(--bg);
      border-top:1px solid var(--line);padding:14px 20px}
 .row{max-width:820px;margin:0 auto;display:flex;gap:10px}
 input{flex:1;padding:11px 14px;font-size:15px;font-family:inherit;color:var(--ink);
       background:var(--panel);border:1px solid var(--line);border-radius:10px}
 input:focus{outline:none;border-color:var(--accent)}
 button{padding:11px 20px;font-size:15px;font-family:inherit;border:0;border-radius:10px;
        background:var(--accent);color:#fff;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 .thinking{color:var(--muted);font-style:italic}

 /* voice */
 .icon{padding:11px 14px;background:var(--panel);color:var(--muted);
       border:1px solid var(--line);border-radius:10px;font-size:15px;line-height:1}
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
  <h1>AirportIQ</h1>
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

function ask(ev){
  if (ev) ev.preventDefault();
  const text = q.value.trim();
  if (!text) return;

  const you = el('div','msg you'); you.appendChild(el('span',null,text));
  log.appendChild(you);
  q.value = ''; go.disabled = true;

  const wrap = el('div','msg');
  const bot = el('div','bot');
  const pending = el('p','thinking','analysing…');
  bot.appendChild(pending); wrap.appendChild(bot); log.appendChild(wrap);
  window.scrollTo(0, document.body.scrollHeight);

  fetch('/v1/chat', {method:'POST', headers:{'content-type':'application/json'},
                     body: JSON.stringify({question:text, session_id:SID})})
    .then(r => r.json())
    .then(d => {
      bot.innerHTML = '';
      const answer = d.answer || d.error || '(no answer)';
      answer.split(/\\n\\n+/).forEach(par => bot.appendChild(el('p',null,par)));

      if (d.intent === 'unsupported')
        bot.querySelector('p').appendChild(el('span','badge warn','outside the data'));

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
      if (speakOn && d.answer) speak(d.answer);
    })
    .catch(e => { bot.innerHTML=''; bot.appendChild(el('p',null,'error: '+e)); })
    .finally(() => { go.disabled=false; q.focus();
                     window.scrollTo(0, document.body.scrollHeight); });
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
