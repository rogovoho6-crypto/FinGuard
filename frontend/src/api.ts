let token = '';
export function setToken(value: string) {token = value;}
export class ApiError extends Error {constructor(public status: number, message: string) {super(message);}}
export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-FinGuard-Token': token}, body: body === undefined ? undefined : JSON.stringify(body)});
  const content = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof content.detail === 'string' ? content.detail : response.status === 422 ? 'Проверьте заполнение полей: значения должны соответствовать указанным ограничениям.' : 'Сервер недоступен. Проверьте, что FinGuard запущен.';
    throw new ApiError(response.status, message);
  }
  return content as T;
}
export const rub = (n: number) => new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2}).format(n);
export const day = (s: string | null | undefined) => s ? s.split('T')[0].split('-').reverse().join('.') : '—';
export const delta = (n: number) => (n > 0 ? '+' : '') + rub(n);
export const compact = (n: number) => new Intl.NumberFormat('ru-RU', {notation:'compact', maximumFractionDigits:1}).format(n);
export function identity(p: import('./types').Payment): import('./types').Identity {return {date:p.payment_date, counterparty:p.counterparty, inn:p.inn, account:p.account, bik:p.bik, amount_kopecks:p.amount_kopecks, reference:p.reference, status:p.status};}
export const emptyParameters = (): import('./types').Parameters => ({payment_actions:[],horizon:30,revenue_drop_pct:0,income_delay_days:0,extra_expense_rub:0});
export function downloadJSON(name: string, content: unknown) {const a = document.createElement('a'); const url=URL.createObjectURL(new Blob([JSON.stringify(content,null,2)],{type:'application/json'})); a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),2000);}
export async function file64(file: File) {if(file.size > 8*1024*1024) throw new Error('Файл превышает 8 МБ.'); const bytes=new Uint8Array(await file.arrayBuffer()); let text=''; for(let i=0;i<bytes.length;i+=8192) text+=String.fromCharCode(...bytes.subarray(i,i+8192)); return btoa(text);}
