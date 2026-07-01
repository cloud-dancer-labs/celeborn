import type { Metadata } from "next";
import "./globals.css";

// The browser/tab title is owned by the client Board component, which renders a React-managed <title>
// carrying the live To Do count (Gmail-style) and the project name. We deliberately do NOT set a title
// here: a second title in metadata would compete with that element. Description-only metadata.
export const metadata: Metadata = {
  description: "A live kanban view of the Celeborn task board (.context/tasks.json).",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
