import "server-only";

import { hash as argonHash, verify as argonVerify } from "@node-rs/argon2";

/**
 * Argon2id. The library exports this as an ambient const enum, which `isolatedModules`
 * forbids importing, so the value is spelled out: 0 = Argon2d, 1 = Argon2i, 2 = Argon2id.
 */
const ARGON2ID = 2;

/**
 * Password hashing, matching `services/api/app/security.py`.
 *
 * Argon2 hashes are a portable PHC string, so hashes written by Python verify here
 * unchanged — no password reset is needed for the migration, and either tier can
 * authenticate a user the other created.
 *
 * Parameters must stay identical to the Python `PasswordHasher`, because a mismatch
 * would make every login trigger a silent rehash and rewrite every stored hash.
 */
export const ARGON2_PARAMS = {
  memoryCost: 65536, // 64 MiB
  timeCost: 3,
  parallelism: 4,
  algorithm: ARGON2ID,
} as const;

export async function hashPassword(password: string): Promise<string> {
  return argonHash(password, ARGON2_PARAMS);
}

/**
 * Verify a password.
 *
 * Mirrors the Python helper's swallow-everything behaviour: a malformed or
 * unrecognised hash is a failed login, never a 500. A corrupt row must not take the
 * endpoint down.
 */
export async function verifyPassword(storedHash: string, password: string): Promise<boolean> {
  try {
    return await argonVerify(storedHash, password);
  } catch {
    return false;
  }
}

/**
 * Whether a stored hash was produced with weaker parameters than we now use.
 *
 * `@node-rs/argon2` exposes no `needsRehash`, so the PHC string is parsed directly:
 *
 *     $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
 *
 * Returns true for anything unparseable, which mirrors argon2-cffi treating an
 * unrecognised hash as needing replacement.
 */
export function needsRehash(storedHash: string): boolean {
  const match = /^\$argon2(id|i|d)\$v=(\d+)\$m=(\d+),t=(\d+),p=(\d+)\$/.exec(storedHash);
  if (match === null) return true;

  const [, variant, , memory, time, parallel] = match;
  return (
    variant !== "id" ||
    Number(memory) !== ARGON2_PARAMS.memoryCost ||
    Number(time) !== ARGON2_PARAMS.timeCost ||
    Number(parallel) !== ARGON2_PARAMS.parallelism
  );
}
