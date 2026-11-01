"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Bot,
  Layers,
  LogIn,
  Mic2,
  ScrollText,
  Settings,
  ShieldCheck,
  User,
  Users,
} from "lucide-react";
import { useAuth } from "@/lib/auth-state";

const navItems = [
  { href: "/", label: "Overview", icon: Layers },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/conversations", label: "Call history", icon: ScrollText },
  { href: "/billing", label: "Billing", icon: Activity },
  { href: "/settings", label: "Settings", icon: Settings },
];

const adminNavItems = [
  { href: "/admin/overview", label: "Overview", icon: Layers },
  { href: "/admin/users", label: "User accounts", icon: Users },
  { href: "/admin/wallet", label: "Wallets", icon: Activity },
  { href: "/admin/agents", label: "Agent inventory", icon: Bot },
  { href: "/admin/sessions", label: "Sessions", icon: Mic2 },
  { href: "/admin/tariffs", label: "Rates & pauses", icon: Settings },
  { href: "/admin/audit", label: "Audit log", icon: ScrollText },
  { href: "/admin/settings", label: "Admin security", icon: ShieldCheck },
];

export function Sidebar() {
  const pathname = usePathname();
  const { state } = useAuth();

  const user = state.user;
  const isAdmin = user?.role === "admin";
  const isAuthenticated = state.status === "authenticated";
  const isPublicRoute = pathname.startsWith("/public/");
  const isAuthRoute =
    pathname === "/login" ||
    pathname.startsWith("/login/") ||
    pathname === "/register" ||
    pathname.startsWith("/register/") ||
    pathname === "/verify-email" ||
    pathname === "/forgot-password" ||
    pathname === "/reset-password";

  if (isPublicRoute) return null;

  const showWorkspaceNav = isAuthenticated && !isAuthRoute;
  const visibleNavItems = showWorkspaceNav ? navItems : [];

  return (
    <aside className="w-full md:w-60 border-b md:border-b-0 md:border-r border-ink-800 bg-ink-950 flex flex-col shrink-0 md:h-screen md:sticky md:top-0 z-40">
      <div className="h-14 md:h-16 px-4 md:px-6 border-b border-ink-800/80 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
            <Mic2 className="w-5 h-5" />
          </div>
          <div>
            <span className="font-semibold text-sm text-white block">Voice AI</span>
            <span className="text-[11px] text-limestone-400 block">Cloud Platform</span>
          </div>
        </div>
      </div>

      <nav
        aria-label="Main navigation"
        className="px-3 py-2 md:p-3 flex md:block gap-1 md:gap-1.5 overflow-x-auto md:overflow-visible md:flex-1"
      >
        {visibleNavItems.map((item) => {
          const Icon = item.icon;
          const isActive =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={isActive ? "page" : undefined}
              className={`flex items-center gap-2 md:gap-3 px-3 py-2 md:px-3.5 md:py-2.5 rounded-lg text-xs md:text-sm font-medium transition-colors whitespace-nowrap shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-400 ${
                isActive
                  ? "bg-ink-800 text-copper-200 border border-ink-700"
                  : "text-limestone-400 hover:text-limestone-100 hover:bg-ink-900"
              }`}
            >
              <Icon
                className={`w-4 h-4 ${
                  isActive ? "text-copper-300" : "text-limestone-400"
                }`}
              />
              {item.label}
            </Link>
          );
        })}

        {showWorkspaceNav && isAdmin && (
          <div className="pt-4 md:pt-5">
            <div className="hidden md:block px-3 py-1.5 text-[11px] font-semibold text-limestone-500 uppercase tracking-wider">
              Administration
            </div>
            <div className="flex md:block gap-1 md:gap-1.5">
              {adminNavItems.map((item) => {
                const Icon = item.icon;
                const isActive = pathname.startsWith(item.href);

                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-current={isActive ? "page" : undefined}
                    className={`flex items-center gap-2 md:gap-3 px-3 py-2 md:px-3.5 md:py-2.5 rounded-lg text-xs md:text-sm font-medium transition-colors whitespace-nowrap shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-400 ${
                      isActive
                        ? "bg-ink-800 text-copper-200 border border-ink-700"
                        : "text-limestone-400 hover:text-limestone-100 hover:bg-ink-900"
                    }`}
                  >
                    <Icon
                      className={`w-4 h-4 ${
                        isActive ? "text-copper-300" : "text-limestone-400"
                      }`}
                    />
                    {item.label}
                  </Link>
                );
              })}
            </div>
          </div>
        )}
      </nav>

      <div className="border-t border-ink-800 p-3">
        {isAuthenticated && user ? (
          <Link
            href="/settings"
            className="flex items-center justify-between p-2 rounded-lg hover:bg-ink-900 transition-colors group"
          >
            <div className="flex items-center gap-2.5 overflow-hidden">
              <div className="w-7 h-7 rounded-md bg-ink-800 border border-ink-700 flex items-center justify-center text-limestone-300 shrink-0">
                <User className="w-3.5 h-3.5" />
              </div>
              <div className="overflow-hidden">
                <span className="text-xs font-medium text-white block truncate">
                  {user.display_name}
                </span>
                <span className="text-[10px] text-limestone-500 block truncate">
                  {user.email}
                </span>
              </div>
            </div>
            <span className="text-[9px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded bg-ink-800 text-limestone-400 border border-ink-700 group-hover:border-copper-700">
              {user.role}
            </span>
          </Link>
        ) : (
          <Link
            href="/login"
            className="flex items-center justify-center gap-2 w-full py-2 px-3 rounded-lg text-xs font-semibold bg-copper-950 text-copper-300 border border-copper-800 hover:bg-copper-900 transition-colors"
          >
            <LogIn className="w-3.5 h-3.5" />
            Sign in
          </Link>
        )}
      </div>
    </aside>
  );
}
