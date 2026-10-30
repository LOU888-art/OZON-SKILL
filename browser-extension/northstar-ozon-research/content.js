const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));

function text(element){return String(element?.innerText||element?.textContent||'').replace(/\s+/g,' ').trim()}
function numberFrom(value){const match=String(value||'').replace(/\u00a0/g,' ').match(/-?[\d\s]+(?:[.,]\d+)?/);return match?match[0].replace(/\s/g,'').replace(',','.') : null}
function productId(url){const path=new URL(url).pathname;const matches=[...path.matchAll(/(\d{5,})/g)];return matches.length?matches[matches.length-1][1]:''}
function headerIndex(headers,needles){return headers.findIndex(header=>needles.some(needle=>header.includes(needle)))}

function parseRows(){
  const tables=[...document.querySelectorAll('table')];
  const rows=[];
  for(const table of tables){
    const headers=[...table.querySelectorAll('thead th')].map(node=>text(node).toLowerCase());
    for(const row of table.querySelectorAll('tbody tr')){
      const cells=[...row.querySelectorAll('td')];
      const link=[...row.querySelectorAll('a[href]')].find(anchor=>/ozon\.ru\/product\//i.test(anchor.href)||/\/product\//i.test(anchor.getAttribute('href')||''));
      if(!link)continue;
      const url=new URL(link.href,location.origin).href;
      const image=row.querySelector('img');
      const cell=(needles)=>{const index=headerIndex(headers,needles);return index>=0?text(cells[index]):''};
      const whole=text(row);
      const brand=(whole.match(/(?:品牌|бренд)\s*[:：]?\s*([^·|]+?)(?=\s+(?:卖家|продавец|货号|артикул)|$)/i)||[])[1]||'';
      const seller=(whole.match(/(?:卖家|продавец)\s*[:：]?\s*([^·|]+?)(?=\s+(?:货号|артикул|品牌|бренд)|$)/i)||[])[1]||'';
      rows.push({
        ozon_product_id:productId(url),
        title:text(link)||text(cells[0]).split(/(?:品牌|бренд|卖家|продавец)/i)[0].trim(),
        product_url:url,
        image_url:image?.currentSrc||image?.src||image?.dataset?.src||'',
        category:cell(['类目','category','категор']),brand:brand.trim(),seller:seller.trim(),
        price_rub:numberFrom(cell(['价格','price','цена'])),
        orders:numberFrom(cell(['已订购商品','订购数量','ordered products','заказано товар','заказы'])),
        revenue_rub:numberFrom(cell(['订购金额','销售额','revenue','оборот','сумма заказ'])),
        sellers:numberFrom(cell(['卖家数量','sellers','продавц'])),
        rating:numberFrom(cell(['评分','rating','рейтинг'])),
        reviews:numberFrom(cell(['评论','reviews','отзыв'])),
        growth_percent:numberFrom(cell(['动态','增长','growth','динамик'])),
        daily_sales:numberFrom(cell(['日销量','daily sales','продаж в день','среднесуточ'])),
        ad_share_percent:numberFrom(cell(['广告费占比','доля рекламных расходов','ad cost share','ддр'])),
        promotion_days:numberFrom(cell(['参与促销天数','дней в акции','promotion days'])),
        promotion_discount_percent:numberFrom(cell(['参与促销的折扣','скидка в акции','promotion discount'])),
        paid_promotion_days:numberFrom(cell(['付费推广天数','дней платного продвижения','paid promotion days'])),
        card_views:numberFrom(cell(['商品卡浏览量','просмотры карточки','product card views','qtyviewpdp'])),
        card_add_to_cart_rate_percent:numberFrom(cell(['商品卡加购率','добавление в корзину из карточки','card add-to-cart'])),
        search_views:numberFrom(cell(['搜索目录浏览量','просмотры в поиске','search views','catalog views'])),
        search_add_to_cart_rate_percent:numberFrom(cell(['搜索目录加购率','добавление в корзину из поиска','search add-to-cart'])),
        conversion_rate_percent:numberFrom(cell(['展示转化率','конверсия показов','display conversion','конверсия в заказ'])),
        return_rate_percent:numberFrom(cell(['退货取消率','возвраты и отмены','return rate','cancel rate'])),
        commission_percent:numberFrom(cell(['类目佣金','комиссия категории','category commission'])),
        days_on_sale:numberFrom(cell(['上架天数','дней на площадке','days on sale'])),
        weight_g:numberFrom(cell(['重量','вес','weight'])),
        delivery_mode:cell(['发货模式','схема работы','delivery mode','fbo','fbs']),
        dimensions:cell(['长宽高','габариты','dimensions','размер упаковки'])
      });
    }
  }
  return rows;
}

function setDateInputs(rules){
  const inputs=[...document.querySelectorAll('input[type="date"]')];
  if(inputs.length<2||!rules)return;
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
  [[inputs[0],rules.date_from],[inputs[1],rules.date_to]].forEach(([input,value])=>{if(!value)return;setter?.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))});
}

async function scan(options){
  setDateInputs(options.rules);
  const found=new Map();
  const limit=Math.max(1,Math.min(500,Number(options.limit)||100));
  const rounds=options.autoScroll?24:1;
  for(let index=0;index<rounds&&found.size<limit;index+=1){
    parseRows().forEach(item=>found.set(item.ozon_product_id||item.product_url,item));
    if(!options.autoScroll)break;
    window.scrollBy({top:Math.max(600,window.innerHeight*.8),behavior:'smooth'});
    const scrollers=[...document.querySelectorAll('div')].filter(node=>node.scrollHeight>node.clientHeight*1.5&&getComputedStyle(node).overflowY!=='visible');
    scrollers.slice(-3).forEach(node=>node.scrollTop=Math.min(node.scrollHeight,node.scrollTop+node.clientHeight*.8));
    await wait(450);
  }
  return [...found.values()].slice(0,limit);
}

chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
  if(message?.type!=='NORTHSTAR_SCAN')return false;
  scan(message).then(items=>sendResponse({ok:true,items})).catch(error=>sendResponse({ok:false,error:error.message}));
  return true;
});
