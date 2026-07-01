/** @type {import('next').NextConfig} */
const nextConfig = {
  // Compile the source-only shared UI package directly (no prebuilt dist). CELE-t98 / t112.
  transpilePackages: ['@celeborn/board-ui'],
  // The board is a read-only viewer; nothing fancy needed.
  // Hide the floating Next.js dev indicator (the circled "N" bottom-left) — it's noise on the board.
  devIndicators: false,
  // Build into a SEPARATE distDir so `next build` never clobbers the live `next dev`'s `.next`
  // (the shared directory used to 500/kill the running board — CELE-t99). Dev keeps the default
  // `.next`; the build script sets NEXT_DIST_DIR=.next-build so the two never share a directory.
  distDir: process.env.NEXT_DIST_DIR || '.next',
};

module.exports = nextConfig;
