import Link from "next/link";

import { DropMark } from "@/components/brand/DropMark";

export default function NotFound() {
  return (
    <div className="grid min-h-dvh place-items-center px-4 text-center">
      <div>
        <DropMark size={44} />
        <h1 className="display mt-6 text-5xl">404</h1>
        <p className="dek mt-3">This story does not exist, or it was taken down.</p>
        <p className="dek mt-1 text-sm">
          If we retracted something you were looking for, it will be on the{" "}
          <Link href="/corrections" className="underline underline-offset-2">
            corrections page
          </Link>
          .
        </p>
        <Link
          href="/"
          className="mt-8 inline-flex h-10 items-center rounded-md bg-accent px-5 text-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover"
        >
          Back to the front page
        </Link>
      </div>
    </div>
  );
}
