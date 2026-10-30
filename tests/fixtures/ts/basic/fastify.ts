import Fastify from "fastify";

const fastify = Fastify();

function fHandler(req, reply) {
  reply.send({ ok: true });
}

function mHandler(req, reply) {
  reply.send({ ok: true });
}

fastify.route({ method: "GET", url: "/f/:fid", handler: fHandler });
fastify.route({ method: ["GET", "POST"], url: "/multi", handler: mHandler });
fastify.get("/direct/:did", fHandler);
