const source = {
  name:'京剧《空城计》选段 · 作品鉴赏',
  url:'https://music.hgnu.edu.cn/_upload/article/files/09/d4/faf43ffb4e08b61cf64ee0adaf16/9e893fdc-b47f-484c-a549-e41df3b03d96.pdf'
};
const commentaries = new Map([
  ['我正在城楼观山景', '诸葛亮把登城说成观赏山景。这句从容的自述先写城楼上的镇定，同时给下一句城外的动静留下反差。'],
  ['耳听得城外乱纷纷', '视角从城楼转到城外，「乱纷纷」写出兵马来临时的声势。把这句和前一句连着听，可以体会外部危局和人物镇定之间的张力。']
]);

export function explainLine(text, genre) {
  if (genre.trim() && !genre.includes('京剧')) return null;
  const key = text.replace(/[\p{P}\s]/gu, '');
  const explanation = commentaries.get(key);
  return explanation ? {explanation, source} : null;
}
