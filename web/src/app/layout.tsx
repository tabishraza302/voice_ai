import type { Metadata } from "next";
import { Sidebar } from "@/components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Voice AI Agent Studio",
  description: "Configure local voice agents, edit prompts, and test browser voice sessions.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-ink-950 text-limestone-100 min-h-screen flex flex-col md:flex-row antialiased">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto md:h-screen">
          <div className="mx-auto w-full max-w-7xl p-4 sm:p-8">{children}</div>
        </main>
      </body>
    </html>
  );
}
