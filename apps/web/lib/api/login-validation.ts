import "server-only";

import type { FieldError } from "@/lib/api/contract";

/**
 * Body validation for POST /auth/login, reproducing Pydantic's error contract.
 *
 * FastAPI's model is:
 *
 *     class LoginRequest(BaseModel):
 *         email: EmailStr
 *         password: str = Field(min_length=1, max_length=256)
 *
 * Messages are Pydantic's and email-validator's exact strings, captured from the
 * running service. Field paths are `body.email` / `body.password`, and every failing
 * field is reported together in declaration order.
 *
 * KNOWN DIVERGENCE RISK: email-validator implements a large rule set (IDNA, quoted
 * local parts, length limits per label, deliverability). This reproduces the checks
 * that the endpoint actually exercises — missing @-sign, empty parts, and
 * special-use/reserved domains — plus a general syntax check. Inputs outside those
 * cases may produce a different *message* (the status is 422 either way).
 * `tests/test_login_parity.py` enumerates a broad set of malformed addresses and
 * fails on any divergence, so the boundary is measured rather than assumed.
 */

/** Reserved / special-use names email-validator refuses. */
const SPECIAL_USE_DOMAINS = new Set([
  "local",
  "localhost",
  "test",
  "invalid",
  "example",
  "alt",
  "onion",
  "internal",
  "home",
  "corp",
  "lan",
]);

const MSG = {
  required: "Field required",
  tooShort: "String should have at least 1 character",
  tooLong: "String should have at most 256 characters",
  notAString: "Input should be a valid string",
  noAtSign: "value is not a valid email address: An email address must have an @-sign.",
  specialUse:
    "value is not a valid email address: The part after the @-sign is a special-use or " +
    "reserved name that cannot be used with email.",
  emptyLocal:
    "value is not a valid email address: There must be something before the @-sign.",
  emptyDomain:
    "value is not a valid email address: There must be something after the @-sign.",
  badDomain:
    "value is not a valid email address: The part after the @-sign is not valid. It should " +
    "have a period.",
  doubleDot: "value is not a valid email address: An email address cannot have two periods in a row.",
  invalidChars:
    "value is not a valid email address: The email address contains invalid characters " +
    "before the @-sign.",
} as const;

export interface LoginBody {
  email: string;
  password: string;
}

export type LoginValidation =
  | { ok: true; value: LoginBody }
  | { ok: false; errors: FieldError[] };

function validateEmail(raw: unknown, errors: FieldError[]): string | null {
  if (raw === undefined || raw === null) {
    errors.push({ field: "body.email", message: MSG.required });
    return null;
  }
  if (typeof raw !== "string") {
    errors.push({ field: "body.email", message: MSG.notAString });
    return null;
  }

  const at = raw.lastIndexOf("@");
  if (at === -1) {
    errors.push({ field: "body.email", message: MSG.noAtSign });
    return null;
  }

  const local = raw.slice(0, at);
  const domain = raw.slice(at + 1);

  if (local.length === 0) {
    errors.push({ field: "body.email", message: MSG.emptyLocal });
    return null;
  }
  if (domain.length === 0) {
    errors.push({ field: "body.email", message: MSG.emptyDomain });
    return null;
  }
  if (/[\s(),:;<>[\]\\]/.test(local)) {
    errors.push({ field: "body.email", message: MSG.invalidChars });
    return null;
  }

  // email-validator reports consecutive periods before it reports a malformed
  // domain, so the order of these two checks is part of the message contract.
  if (raw.includes("..")) {
    errors.push({ field: "body.email", message: MSG.doubleDot });
    return null;
  }

  const labels = domain.split(".");
  const tld = labels[labels.length - 1]?.toLowerCase() ?? "";

  if (labels.length < 2 || labels.some((l) => l.length === 0)) {
    errors.push({ field: "body.email", message: MSG.badDomain });
    return null;
  }
  if (SPECIAL_USE_DOMAINS.has(tld)) {
    errors.push({ field: "body.email", message: MSG.specialUse });
    return null;
  }

  return raw;
}

function validatePassword(raw: unknown, errors: FieldError[]): string | null {
  if (raw === undefined || raw === null) {
    errors.push({ field: "body.password", message: MSG.required });
    return null;
  }
  if (typeof raw !== "string") {
    errors.push({ field: "body.password", message: MSG.notAString });
    return null;
  }
  if (raw.length < 1) {
    errors.push({ field: "body.password", message: MSG.tooShort });
    return null;
  }
  if (raw.length > 256) {
    errors.push({ field: "body.password", message: MSG.tooLong });
    return null;
  }
  return raw;
}

/** Fields are validated in declaration order — email, then password — like Pydantic. */
export function validateLoginBody(body: unknown): LoginValidation {
  const errors: FieldError[] = [];
  const record = (body ?? {}) as Record<string, unknown>;

  const email = validateEmail(record.email, errors);
  const password = validatePassword(record.password, errors);

  if (errors.length > 0 || email === null || password === null) {
    return { ok: false, errors };
  }
  return { ok: true, value: { email, password } };
}
