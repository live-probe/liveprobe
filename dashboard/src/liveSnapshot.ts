import type { Incident } from './api.ts';

export const OPEN_STATES = ['open', 'analyzing', 'action_pending', 'acting'] as const;
type Page = { items: Incident[]; total: number };
/** 최근 기록의 개수와 관계없이 진행 중 장애를 조회한다. 부분 결과는 정상 목록으로 내보내지 않는다. */
export async function loadOpenIncidents(load: (path: string) => Promise<Page>): Promise<Incident[]> {
  const lists = await Promise.all(OPEN_STATES.map(async status => {
    const items: Incident[] = [];
    for (let page = 0; page < 100; page++) {
      const d = await load(`/incidents?status=${status}&limit=500&offset=${items.length}`);
      if (!Array.isArray(d.items) || !Number.isInteger(d.total) || d.total < 0) throw new Error('장애 목록 응답 형식 오류');
      if (!d.items.length && items.length < d.total) throw new Error('진행 중 장애 목록이 일부만 조회되었습니다.');
      items.push(...d.items);
      if (items.length >= d.total) return items;
    }
    throw new Error('진행 중 장애 목록 조회 한도를 넘었습니다.');
  }));
  return [...new Map(lists.flat().map(i => [i.id, i])).values()].filter(i => (OPEN_STATES as readonly string[]).includes(i.status)).sort((a, b) => b.id - a.id);
}
