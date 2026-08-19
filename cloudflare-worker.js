// Cloudflare Worker: принимает вебхук от ЮKassa и пересылает событие в приватный канал
// Discord (через channel webhook), откуда его подхватывает бот. Сам вебхук ЮKassa
// ничем не подписан, поэтому воркер не является источником истины — бот, получив
// сообщение, дополнительно сверяет реальный статус платежа напрямую в API ЮKassa.
//
// Настройка секрета: Cloudflare Dashboard → ваш Worker → Settings → Variables →
// добавить Secret variable с именем DISCORD_WEBHOOK_URL.

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response("bad request", { status: 400 });
    }

    const paymentId = body && body.object && body.object.id;
    const event = (body && body.event) || "unknown";

    if (!paymentId) {
      return new Response("ok", { status: 200 });
    }

    await fetch(env.DISCORD_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: `yookassa:${event}:${paymentId}` }),
    });

    return new Response("ok", { status: 200 });
  },
};
