import fs from "node:fs";
import path from "node:path";
import { execFileSync, execSync } from "node:child_process";
import crypto from "node:crypto";

// Resolve repository/worktree root
export const WORKTREE_ROOT = path.resolve(__dirname, "../../..");

/**
 * Return the verifier-owned mail directory from PLATFORM_AUTH_MAIL_DIR if set,
 * falling back to local development data/platform-auth-mail.
 */
export function getMailDir(): string {
  if (process.env.PLATFORM_AUTH_MAIL_DIR) {
    return path.resolve(process.env.PLATFORM_AUTH_MAIL_DIR);
  }
  return path.join(WORKTREE_ROOT, "data", "platform-auth-mail");
}

export interface RawMailFileContent {
  mail_id: string;
  to: string;
  purpose: "register" | "password_reset";
  link: string;
  created_at: string;
}

export interface AuthMailMessage {
  mailId: string;
  to: string;
  purpose: "register" | "password_reset";
  link: string;
  token: string;
  createdAt: string;
}

/**
 * Trigger the transactional mail dispatcher CLI command to flush queued
 * messages to the configured development mail directory.
 */
export function triggerMailDelivery(): void {
  try {
    execFileSync(
      "uv",
      ["run", "python", "-m", "platform_api.cli", "deliver-auth-mail", "--once", "--limit", "50"],
      {
        cwd: WORKTREE_ROOT,
        stdio: "pipe",
        env: {
          ...process.env,
        },
      }
    );
  } catch (err: unknown) {
    const errorOutput = err instanceof Error && "stderr" in err ? String(err.stderr) : String(err);
    console.warn(`[triggerMailDelivery warning]: ${errorOutput}`);
  }
}

/**
 * Scan the verifier-owned mail directory and read the latest transactional email
 * dispatched to the target email address for the given purpose.
 */
export function getLatestMail(options: {
  email: string;
  purpose?: "register" | "password_reset";
  maxWaitMs?: number;
}): AuthMailMessage {
  const mailDir = getMailDir();
  const targetEmail = options.email.trim().toLowerCase();
  const targetPurpose = options.purpose;
  const timeout = options.maxWaitMs || 5000;
  const startTime = Date.now();

  while (Date.now() - startTime < timeout) {
    triggerMailDelivery();

    if (fs.existsSync(mailDir)) {
      const files = fs.readdirSync(mailDir).filter((f) => f.endsWith(".json"));

      const matching: Array<{
        file: string;
        data: RawMailFileContent;
        mtime: number;
      }> = [];

      for (const file of files) {
        try {
          const filePath = path.join(mailDir, file);
          const stat = fs.statSync(filePath);
          const content = JSON.parse(
            fs.readFileSync(filePath, "utf-8")
          ) as RawMailFileContent;

          if (
            content.to &&
            content.to.toLowerCase() === targetEmail &&
            (!targetPurpose || content.purpose === targetPurpose)
          ) {
            matching.push({ file, data: content, mtime: stat.mtimeMs });
          }
        } catch {
          // File might be mid-write or invalid JSON
        }
      }

      if (matching.length > 0) {
        // Sort newest first
        matching.sort((a, b) => b.mtime - a.mtime);
        const latest = matching[0].data;

        // Extract token from URL fragment in link (e.g. #token=...)
        let token = "";
        const hashIndex = latest.link.indexOf("#");
        if (hashIndex !== -1) {
          const fragment = latest.link.substring(hashIndex + 1);
          const params = new URLSearchParams(fragment);
          token = params.get("token") || "";
        }

        return {
          mailId: latest.mail_id,
          to: latest.to,
          purpose: latest.purpose,
          link: latest.link,
          token,
          createdAt: latest.created_at,
        };
      }
    }

    // Short sleep before checking again
    execSync("sleep 0.2");
  }

  throw new Error(
    `No transactional email found in ${mailDir} for ${targetEmail} (${targetPurpose}) within ${timeout}ms`
  );
}

/**
 * Seed an active administrator account directly into test PostgreSQL via secure
 * standard input piping (never exposing credentials or passwords in argv/process list).
 */
export function createAdminUser(options: {
  email: string;
  displayName: string;
  password?: string;
}): void {
  const password = options.password || "AdminMasterPassword12345";
  const payload = JSON.stringify({
    email: options.email.trim().toLowerCase(),
    display_name: options.displayName.trim(),
    password,
  });

  const pythonScript = [
    "import sys, json, asyncio, uuid",
    "from datetime import datetime, timezone",
    "from platform_api.config import PlatformSettings",
    "from platform_api.db.session import create_engine, get_session_factory",
    "from platform_api.models.auth import User, AuthMfa",
    "from platform_api.accounts.service import initialize_user_account",
    "from platform_api.auth.passwords import get_password_hasher",
    "",
    "data = json.loads(sys.stdin.read())",
    "settings = PlatformSettings()",
    "engine = create_engine(settings)",
    "session_factory = get_session_factory(engine)",
    "",
    "async def seed():",
    "    hasher = get_password_hasher(settings.auth_hash_concurrency)",
    "    pw_hash = await hasher.hash_password(data['password'])",
    "    now = datetime.now(timezone.utc)",
    "    async with session_factory() as session:",
    "        user = User(",
    "            id=uuid.uuid4(),",
    "            email=data['email'],",
    "            email_key=data['email'],",
    "            display_name=data['display_name'],",
    "            password_hash=pw_hash,",
    "            role='admin',",
    "            status='active',",
    "            email_verified_at=now,",
    "            preferred_language='en',",
    "            timezone='UTC',",
    "            low_balance_notifications=True,",
    "        )",
    "        session.add(user)",
    "        await session.flush()",
    "        mfa_record = AuthMfa(",
    "            user_id=user.id,",
    "            secret_ciphertext=None,",
    "            pending_secret_ciphertext=None,",
    "            pending_expires_at=None,",
    "            enabled_at=None,",
    "            last_accepted_step=None,",
    "        )",
    "        session.add(mfa_record)",
    "        await session.flush()",
    "        await initialize_user_account(session, user_id=user.id, now=now)",
    "        await session.commit()",
    "",
    "asyncio.run(seed())",
  ].join("\n");

  execFileSync("uv", ["run", "python", "-c", pythonScript], {
    cwd: WORKTREE_ROOT,
    input: payload,
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
    },
  });
}

/**
 * Generate RFC 6238 compatible 6-digit TOTP code from base32 secret.
 * Supports optional offsetSteps for testing sequential valid timesteps
 * within the ±1 step window without wall-clock sleep delays.
 */
export function generateTotpCode(
  secretBase32: string,
  offsetSteps: number = 0
): string {
  const cleanSecret = secretBase32.replace(/[\s=]/g, "").toUpperCase();
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

  let bits = "";
  for (let i = 0; i < cleanSecret.length; i++) {
    const val = alphabet.indexOf(cleanSecret[i]);
    if (val === -1) continue;
    bits += val.toString(2).padStart(5, "0");
  }

  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.substring(i, i + 8), 2));
  }
  const key = Buffer.from(bytes);

  // 30-second time window counter with optional offset steps
  const epoch = Math.floor(Date.now() / 1000);
  const timeStep = Math.floor(epoch / 30) + offsetSteps;
  const timeBuffer = Buffer.alloc(8);
  timeBuffer.writeBigInt64BE(BigInt(timeStep));

  const hmac = crypto.createHmac("sha1", key);
  hmac.update(timeBuffer);
  const digest = hmac.digest();

  const offset = digest[digest.length - 1] & 0x0f;
  const codeNum =
    ((digest[offset] & 0x7f) << 24) |
    ((digest[offset + 1] & 0xff) << 16) |
    ((digest[offset + 2] & 0xff) << 8) |
    (digest[offset + 3] & 0xff);

  return (codeNum % 1_000_000).toString().padStart(6, "0");
}

/**
 * Force the active session for an administrator to have a stale step-up timestamp
 * (10 minutes in the past), exercising real require_step_up failure and in-browser
 * StepUpDialog resolution without relying on synthetic mocks.
 */
export function makeAdminSessionStepUpStale(options: { email: string }): void {
  const pythonScript = [
    "import sys, asyncio",
    "from datetime import datetime, timezone, timedelta",
    "from sqlalchemy import select, update",
    "from platform_api.config import PlatformSettings",
    "from platform_api.db.session import create_engine, get_session_factory",
    "from platform_api.models.auth import User, AuthSession",
    "",
    "email = sys.argv[1].strip().lower()",
    "settings = PlatformSettings()",
    "engine = create_engine(settings)",
    "session_factory = get_session_factory(engine)",
    "",
    "async def make_stale():",
    "    async with session_factory() as session:",
    "        user = (await session.execute(select(User).where(User.email_key == email))).scalar_one_or_none()",
    "        if not user:",
    "            raise RuntimeError(f'user {email} not found')",
    "        stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)",
    "        await session.execute(",
    "            update(AuthSession)",
    "            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))",
    "            .values(step_up_at=stale_time)",
    "        )",
    "        await session.commit()",
    "",
    "asyncio.run(make_stale())",
  ].join("\n");

  execFileSync("uv", ["run", "python", "-c", pythonScript, options.email], {
    cwd: WORKTREE_ROOT,
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
    },
  });
}
