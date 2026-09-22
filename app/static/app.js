const $=id=>document.getElementById(id);
const API=window.location.origin;
const poster=p=>p?`https://image.tmdb.org/t/p/w500${p}`:'';
const esc=s=>String(s??'').replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#039;'}[m]));
const fmt=n=>new Intl.NumberFormat('pl-PL').format(n||0);

const countryNames={
  US:'🇺🇸 USA',GB:'🇬🇧 Wielka Brytania',DK:'🇩🇰 Dania',PL:'🇵🇱 Polska',DE:'🇩🇪 Niemcy',
  FR:'🇫🇷 Francja',ES:'🇪🇸 Hiszpania',IT:'🇮🇹 Włochy',KR:'🇰🇷 Korea Południowa',
  JP:'🇯🇵 Japonia',IN:'🇮🇳 Indie',CA:'🇨🇦 Kanada',AU:'🇦🇺 Australia',SE:'🇸🇪 Szwecja',
  NO:'🇳🇴 Norwegia',FI:'🇫🇮 Finlandia',NL:'🇳🇱 Holandia',BE:'🇧🇪 Belgia',IE:'🇮🇪 Irlandia',
  AT:'🇦🇹 Austria',CH:'🇨🇭 Szwajcaria',PT:'🇵🇹 Portugalia',CZ:'🇨🇿 Czechy',GR:'🇬🇷 Grecja',
  TR:'🇹🇷 Turcja',IS:'🇮🇸 Islandia',RO:'🇷🇴 Rumunia',UA:'🇺🇦 Ukraina',HR:'🇭🇷 Chorwacja',
  MX:'🇲🇽 Meksyk',BR:'🇧🇷 Brazylia',AR:'🇦🇷 Argentyna',CO:'🇨🇴 Kolumbia',CL:'🇨🇱 Chile',
  UY:'🇺🇾 Urugwaj',TH:'🇹🇭 Tajlandia',ID:'🇮🇩 Indonezja',TW:'🇹🇼 Tajwan',PH:'🇵🇭 Filipiny',
  HK:'🇭🇰 Hongkong',NZ:'🇳🇿 Nowa Zelandia',ZA:'🇿🇦 RPA',NG:'🇳🇬 Nigeria',IL:'🇮🇱 Izrael'
};

function params(){
  const p=new URLSearchParams();
  const c=$("country").value;
  if(c)p.set('production_country',c);
  p.set('content_type',$("type").value);
  p.set('min_rating',$("rating").value);
  p.set('min_votes',$("votes").value);
  p.set('sort',$("sort").value);
  p.set('region','DK');
  p.set('limit',$("limit").value);
  return p;
}
function sortLabel(v){return ({rating:'ocenie',votes:'liczbie głosów',popularity:'popularności'})[v]}

function render(items){
  const root=$("results");
  if(!items.length){root.innerHTML='<div class="empty">Nie znaleziono tytułów spełniających te kryteria.</div>';return}
  root.innerHTML=items.map((x,i)=>`
    <article class="result-card" data-type="${x.type}" data-id="${x.id}">
      <div class="poster-wrap">
        ${x.poster_path?`<img loading="lazy" src="${poster(x.poster_path)}" alt="${esc(x.title)}">`:'<div class="loading">Brak plakatu</div>'}
        <span class="rank">#${i+1}</span>
        <span class="score">⭐ ${Number(x.rating||0).toFixed(1)}</span>
      </div>
      <div class="info">
        <div class="title">${esc(x.title)}</div>
        <div class="meta">${esc(x.year||'')} · ${fmt(x.vote_count)} głosów<br>${x.type==='movie'?'Film':x.type==='miniseries'?'Miniserial':'Serial'}</div>
      </div>
    </article>`).join('');
  root.querySelectorAll('.result-card').forEach(c=>c.addEventListener('click',()=>openDetails(c.dataset.type,c.dataset.id)));
}

async function search(){
  const btn=$("searchBtn");
  btn.disabled=true;
  $("status").textContent='Szukam…';
  $("results").innerHTML='<div class="loading">Pobieranie wyników…</div>';
  try{
    const r=await fetch(`${API}/search?${params()}`);
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const d=await r.json();
    $("resultsTitle").textContent=`TOP ${d.results.length}`;
    $("resultCount").textContent=`Netflix · DK · sortowanie po ${sortLabel(d.criteria.sort)}`;
    $("status").textContent='';
    render(d.results);
  }catch(e){
    $("results").innerHTML='<div class="empty">Nie udało się pobrać wyników. Spróbuj ponownie.</div>';
    $("status").textContent=e.message;
  }finally{btn.disabled=false}
}

function typeLabel(t){return t==='movie'?'Film':t==='miniseries'?'Miniserial':'Serial'}

async function openDetails(type,id){
  const modal=$("modal"),content=$("modalContent");
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden','false');
  content.innerHTML='<div class="loading">Ładowanie szczegółów…</div>';

  const media=type==='movie'?'movie':'tv';
  try{
    const r=await fetch(`${API}/title/${media}/${id}`);
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const x=await r.json();

    const countries=(x.production_countries||[]).map(c=>countryNames[c]||c).join(' · ');
    const netflix=x.netflix_available_in_default_region;
    const runtime=x.runtime_minutes?`${x.runtime_minutes} min`:null;

    const metricsAvailable=x.netflix_rank!=null || x.netflix_views || x.hours_viewed;
    const metricsWeek=x.netflix_metrics_week?`Tydzień od ${x.netflix_metrics_week}`:'';
    const metricsNote=metricsAvailable
      ? `Dane z Netflix Top 10${metricsWeek?` · ${metricsWeek}`:''}.`
      : 'Ten tytuł nie pojawił się w znalezionych danych Netflix Top 10 dla ostatniego pełnego tygodnia.';

    content.innerHTML=`
      <div class="detail">
        <div class="detail-poster">
          ${x.poster_url?`<img src="${x.poster_url}" alt="${esc(x.title)}">`:'<div class="detail-no-poster">Brak plakatu</div>'}
        </div>
        <div class="detail-body">
          <div class="detail-kicker">${typeLabel(x.type)} · Netflix DK</div>
          <h3>${esc(x.title)}</h3>
          ${x.original_title && x.original_title!==x.title?`<div class="original-title">${esc(x.original_title)}</div>`:''}
          <div class="detail-stats">
            <span>⭐ <b>${Number(x.rating||0).toFixed(1)}</b></span>
            <span>👥 ${fmt(x.vote_count)}</span>
            ${x.year?`<span>📅 ${esc(x.year)}</span>`:''}
            ${runtime?`<span>⏱ ${runtime}</span>`:''}
          </div>
          <div class="availability ${netflix?'available':'unavailable'}">
            ${netflix?'✓ Dostępne na Netflix w Danii':'Nie potwierdzono dostępności na Netflix w Danii'}
          </div>
          ${countries?`<div class="detail-country">${countries}</div>`:''}
          <p class="detail-overview">${esc(x.overview||'Brak opisu.')}</p>

          <div class="netflix-metrics">
            <div class="metrics-title">NETFLIX TOP 10</div>
            <div class="metrics-grid">
              <div><strong>${x.netflix_rank??'—'}</strong><small>Pozycja DK</small></div>
              <div><strong>${x.netflix_views??'—'}</strong><small>Views</small></div>
              <div><strong>${x.hours_viewed??'—'}</strong><small>Hours Viewed</small></div>
            </div>
            <div class="metrics-note">${esc(metricsNote)}</div>
          </div>

          <a class="tmdb-link" target="_blank" rel="noopener" href="${x.tmdb_url}">Otwórz w TMDB ↗</a>
        </div>
      </div>`;
  }catch(e){
    content.innerHTML='<div class="empty">Nie udało się pobrać szczegółów.</div>';
  }
}

function closeModal(){
  const m=$("modal");
  m.classList.add('hidden');
  m.setAttribute('aria-hidden','true');
}

$("searchBtn").addEventListener('click',search);
document.querySelectorAll('[data-close]').forEach(x=>x.addEventListener('click',closeModal));
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
search();

const installHint=$("installHint");
const dismissInstall=$("dismissInstall");
if(installHint && dismissInstall){
  if(window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone===true){
    installHint.classList.add("hidden");
  }else if(!localStorage.getItem("nf_install_hint_dismissed")){
    installHint.classList.remove("hidden");
  }
  dismissInstall.addEventListener("click",()=>{
    localStorage.setItem("nf_install_hint_dismissed","1");
    installHint.classList.add("hidden")
  });
}
