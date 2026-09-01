// THE DROP — PM2 process definitions for the unprivileged deployment (ADR-0012).
//
//   pm2 start infrastructure/pm2/ecosystem.config.cjs
//   pm2 save                 # writes the dump that `pm2 resurrect` restores at @reboot
//   pm2 list / pm2 logs thedrop-api / pm2 monit
//
// PM2 rather than systemd because this host gives us no root. PM2 rather than a
// hand-rolled supervisor because PM2 was already installed here, already had a
// `@reboot ... pm2 resurrect` crontab entry, and gives back most of what systemd
// provided: restart backoff, a per-process memory ceiling, log capture and a status
// view. Two supervisors for one job would be the worse choice.
//
// Every process is launched through `bash -lc` so that:
//   * the env file is parsed by bash, exactly as deploy-userspace.sh parses it -- one
//     parser, one set of quoting rules, no second implementation to disagree with it;
//   * `-l` picks up nvm's PATH, without which `node` is missing under cron at reboot.
// `exec` replaces the shell so PM2 supervises the real process, not a wrapper that
// would report healthy while its child was dead.

const HOME = process.env.HOME;
const APP = `${HOME}/thedrop`;
const ENV_FILE = `${HOME}/.config/thedrop/thedrop.env`;
const REDIS_CONF = `${HOME}/.config/thedrop/redis.conf`;

// `set -a` exports everything the file defines; `set +a` stops that leaking into the
// exec'd command's own assignments.
const withEnv = (cmd) => `set -a; . ${ENV_FILE}; set +a; ${cmd}`;

const common = {
  interpreter: "none",
  instances: 1,
  autorestart: true,
  // Back off rather than hammer a process that cannot start. Roughly systemd's
  // RestartSec/StartLimitBurst pairing.
  restart_delay: 5000,
  exp_backoff_restart_delay: 200,
  max_restarts: 10,
  min_uptime: 20000,
  merge_logs: true,
  time: true,
  out_file: `${HOME}/.local/state/thedrop/log/%name%.log`,
  error_file: `${HOME}/.local/state/thedrop/log/%name%.err.log`,
};

module.exports = {
  apps: [
    {
      // Our own instance on 6380. CloudPanel's Redis on 6379 is untouched: it is shared
      // with PHP sites under an eviction policy, and this application keeps admin
      // sessions and login rate-limit counters in Redis.
      ...common,
      name: "thedrop-redis",
      script: "bash",
      args: ["-lc", `exec redis-server ${REDIS_CONF}`],
      // maxmemory in redis.conf caps the dataset at 256mb; this is the backstop.
      max_memory_restart: "400M",
    },
    {
      ...common,
      name: "thedrop-api",
      script: "bash",
      args: [
        "-lc",
        withEnv(
          `cd ${APP}/services/api && exec ${APP}/.venv/bin/uvicorn app.main:app ` +
            `--host 127.0.0.1 --port 8000 --workers 2 --no-access-log ` +
            `--proxy-headers --forwarded-allow-ips=127.0.0.1`,
        ),
      ],
      max_memory_restart: "700M",
    },
    {
      // -B embeds the beat scheduler.
      //
      // HARD RULE: never more than one instance. Two means every scheduled task fires
      // twice -- duplicate ingestion, duplicate publishing, duplicate spend. `instances:
      // 1` plus PM2's unique-name constraint is what enforces that here; systemd did it
      // before. If a second worker is ever needed, split beat into its own app FIRST.
      // See ADR-0003.
      ...common,
      name: "thedrop-worker",
      script: "bash",
      args: [
        "-lc",
        withEnv(
          `cd ${APP}/services/worker && exec ${APP}/.venv/bin/celery -A app.celery_app ` +
            `worker -B -Q ingest,maintain,publish --concurrency=2 --loglevel=INFO ` +
            `--without-gossip --without-mingle`,
        ),
      ],
      max_memory_restart: "700M",
    },
    {
      // Bound to loopback. CloudPanel's nginx proxies to it; 3100 is never public.
      ...common,
      name: "thedrop-web",
      script: "bash",
      args: [
        "-lc",
        withEnv(
          `cd ${APP}/apps/web/.next/standalone/apps/web && ` +
            `HOSTNAME=127.0.0.1 PORT=3100 NODE_ENV=production exec node server.js`,
        ),
      ],
      max_memory_restart: "800M",
    },
  ],
};
