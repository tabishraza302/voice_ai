"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-state";
import { platformApi, PlatformApiError } from "@/lib/platform-api-client";
import type { UserStatus, UserView } from "@/types/auth";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Filter,
  Loader2,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
  UserX,
  Users,
  X,
} from "lucide-react";

function AdminUsersContent() {
  const { executeWithStepUp } = useAuth();

  const [users, setUsers] = useState<UserView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Search & Filters
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | UserStatus>("all");
  const [cursor, setCursor] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  // Action Dialog state
  const [selectedUser, setSelectedUser] = useState<UserView | null>(null);
  const [actionType, setActionType] = useState<"suspend" | "restore" | null>(null);
  const [reason, setReason] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Detail Modal state
  const [detailUser, setDetailUser] = useState<UserView | null>(null);

  const fetchUsers = useCallback(async (targetCursor: string | null = null) => {
    setLoading(true);
    setError(null);
    try {
      const page = await platformApi.adminGetUsers({
        limit: 25,
        cursor: targetCursor,
        q: query.trim() || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
      });
      setUsers(page.items);
      setNextCursor(page.next_cursor);
      setCursor(targetCursor);
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        setError(err.message);
      } else {
        setError("Failed to load user accounts");
      }
    } finally {
      setLoading(false);
    }
  }, [query, statusFilter]);

  useEffect(() => {
    fetchUsers(null);
  }, [fetchUsers]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchUsers(null);
  };

  const handleOpenAction = (user: UserView, type: "suspend" | "restore") => {
    setSelectedUser(user);
    setActionType(type);
    setReason("");
    setActionError(null);
  };

  const handleCloseAction = () => {
    if (actionLoading) return;
    setSelectedUser(null);
    setActionType(null);
    setReason("");
    setActionError(null);
  };

  const handleSubmitAction = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedUser || !actionType || actionLoading) return;

    const trimmedReason = reason.trim();
    if (trimmedReason.length < 10) {
      setActionError("Reason must be at least 10 characters long");
      return;
    }
    if (trimmedReason.length > 500) {
      setActionError("Reason cannot exceed 500 characters");
      return;
    }

    setActionLoading(true);
    setActionError(null);

    try {
      const updatedUser = await executeWithStepUp(async () => {
        if (actionType === "suspend") {
          return await platformApi.adminSuspendUser(selectedUser.id, {
            reason: trimmedReason,
          });
        } else {
          return await platformApi.adminRestoreUser(selectedUser.id, {
            reason: trimmedReason,
          });
        }
      });

      // Update in local list
      setUsers((prev) =>
        prev.map((u) => (u.id === updatedUser.id ? updatedUser : u))
      );
      if (detailUser?.id === updatedUser.id) {
        setDetailUser(updatedUser);
      }
      handleCloseAction();
    } catch (err: unknown) {
      if (err instanceof PlatformApiError) {
        if (err.code === "LAST_ADMIN") {
          setActionError("Cannot suspend the last active administrator");
        } else if (err.code === "STEP_UP_REQUIRED") {
          setActionError("Admin step-up authentication is required");
        } else {
          setActionError(err.message || `Failed to ${actionType} user`);
        }
      } else {
        setActionError(err instanceof Error ? err.message : "Action failed");
      }
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-ink-800 pb-5">
        <div>
          <div className="flex items-center gap-2">
            <Users className="w-6 h-6 text-copper-400" />
            <h1 className="text-2xl font-bold text-white">User Management</h1>
          </div>
          <p className="text-sm text-limestone-400 mt-1">
            Search, inspect, suspend, or restore platform customer and administrator accounts
          </p>
        </div>
        <button
          type="button"
          onClick={() => fetchUsers(cursor)}
          disabled={loading}
          className="button-secondary text-xs self-start sm:self-auto inline-flex items-center gap-1.5"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </header>

      {/* Filter and search bar */}
      <div className="surface p-4 flex flex-col md:flex-row gap-3 items-stretch md:items-center justify-between">
        <form onSubmit={handleSearchSubmit} className="flex-1 flex gap-2">
          <div className="relative flex-1">
            <Search className="w-4 h-4 absolute left-3 top-3 text-limestone-500" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by email or display name..."
              className="field-control pl-9 text-xs sm:text-sm"
              maxLength={80}
            />
          </div>
          <button type="submit" className="button-secondary text-xs px-3">
            Search
          </button>
        </form>

        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-limestone-400" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as "all" | UserStatus)}
            className="field-control text-xs sm:text-sm w-auto"
          >
            <option value="all">All statuses</option>
            <option value="active">Active only</option>
            <option value="suspended">Suspended only</option>
          </select>
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="surface flex items-start gap-3 border-rust-800 bg-rust-950/40 p-4 text-xs sm:text-sm text-rust-200"
        >
          <ShieldAlert className="w-5 h-5 text-rust-400 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Users table */}
      <div className="surface overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs sm:text-sm">
            <thead className="bg-ink-900 border-b border-ink-800 text-limestone-400 uppercase text-[11px] font-semibold tracking-wider">
              <tr>
                <th className="px-4 py-3">User</th>
                <th className="px-4 py-3">Role</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">MFA</th>
                <th className="px-4 py-3">Created</th>
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800/60">
              {loading && users.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-limestone-400">
                    <Loader2 className="w-5 h-5 animate-spin mx-auto text-copper-400 mb-2" />
                    Loading user accounts...
                  </td>
                </tr>
              ) : users.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-limestone-400">
                    No accounts found matching your query.
                  </td>
                </tr>
              ) : (
                users.map((user) => (
                  <tr key={user.id} className="hover:bg-ink-900/40 transition-colors">
                    <td className="px-4 py-3">
                      <div className="font-medium text-white">{user.display_name}</div>
                      <div className="text-[11px] text-limestone-400">{user.email}</div>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider ${
                          user.role === "admin"
                            ? "bg-copper-950 text-copper-300 border border-copper-800"
                            : "bg-ink-800 text-limestone-300"
                        }`}
                      >
                        {user.role}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider ${
                          user.status === "active"
                            ? "bg-moss-950 text-moss-300 border border-moss-800"
                            : "bg-rust-950 text-rust-300 border border-rust-800"
                        }`}
                      >
                        {user.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {user.mfa_enabled ? (
                        <span className="flex items-center gap-1 text-[11px] text-moss-400 font-medium">
                          <ShieldCheck className="w-3.5 h-3.5" />
                          Enabled
                        </span>
                      ) : (
                        <span className="text-[11px] text-limestone-500">None</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-[11px] text-limestone-400 whitespace-nowrap">
                      {new Date(user.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          onClick={() => setDetailUser(user)}
                          className="button-secondary text-xs px-2.5 py-1"
                        >
                          Details
                        </button>
                        {user.status === "active" ? (
                          <button
                            type="button"
                            onClick={() => handleOpenAction(user, "suspend")}
                            className="button-secondary text-xs px-2.5 py-1 text-rust-300 hover:border-rust-800"
                          >
                            Suspend
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => handleOpenAction(user, "restore")}
                            className="button-secondary text-xs px-2.5 py-1 text-moss-300 hover:border-moss-800"
                          >
                            Restore
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination bar */}
        <div className="border-t border-ink-800 px-4 py-3 flex items-center justify-between text-xs text-limestone-400">
          <div>Showing {users.length} accounts</div>
          {nextCursor && (
            <button
              type="button"
              onClick={() => fetchUsers(nextCursor)}
              disabled={loading}
              className="button-secondary text-xs inline-flex items-center gap-1"
            >
              Next Page
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Suspend / Restore Action Dialog */}
      {selectedUser && actionType && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="action-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs"
        >
          <div className="surface w-full max-w-md p-6 space-y-4 border-copper-700/60 shadow-2xl relative">
            <button
              type="button"
              onClick={handleCloseAction}
              disabled={actionLoading}
              aria-label="Close dialog"
              className="absolute top-4 right-4 text-limestone-400 hover:text-white"
            >
              <X className="w-5 h-5" />
            </button>

            <div className="flex items-center gap-3">
              <div
                className={`w-10 h-10 rounded-lg flex items-center justify-center ${
                  actionType === "suspend"
                    ? "bg-rust-950 border border-rust-800 text-rust-300"
                    : "bg-moss-950 border border-moss-800 text-moss-300"
                }`}
              >
                {actionType === "suspend" ? (
                  <UserX className="w-5 h-5" />
                ) : (
                  <UserCheck className="w-5 h-5" />
                )}
              </div>
              <div>
                <h2 id="action-title" className="text-base font-semibold text-white">
                  {actionType === "suspend" ? "Suspend Account" : "Restore Account"}
                </h2>
                <p className="text-xs text-limestone-400">
                  Target: {selectedUser.display_name} ({selectedUser.email})
                </p>
              </div>
            </div>

            {actionError && (
              <div
                role="alert"
                className="surface border-rust-800 bg-rust-950/40 p-3 text-xs text-rust-200"
              >
                {actionError}
              </div>
            )}

            <form onSubmit={handleSubmitAction} className="space-y-4">
              <div>
                <label htmlFor="action-reason" className="field-label">
                  Audit reason (10–500 characters)
                </label>
                <textarea
                  id="action-reason"
                  required
                  rows={3}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  disabled={actionLoading}
                  placeholder={`Provide the administrative reason to ${actionType} this user account...`}
                  className="field-control resize-none"
                  minLength={10}
                  maxLength={500}
                />
                <div className="text-[11px] text-limestone-500 mt-1 text-right">
                  {reason.trim().length} / 500 characters (min 10)
                </div>
              </div>

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={handleCloseAction}
                  disabled={actionLoading}
                  className="button-secondary"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={actionLoading || reason.trim().length < 10}
                  className={`button-primary ${
                    actionType === "suspend"
                      ? "bg-rust-700 hover:bg-rust-600 text-white"
                      : ""
                  }`}
                >
                  {actionLoading ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Executing...
                    </>
                  ) : actionType === "suspend" ? (
                    "Confirm Suspension"
                  ) : (
                    "Confirm Restore"
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* User Details Modal */}
      {detailUser && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="detail-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs"
        >
          <div className="surface w-full max-w-lg p-6 space-y-4 border-ink-700 shadow-2xl relative">
            <button
              type="button"
              onClick={() => setDetailUser(null)}
              aria-label="Close details"
              className="absolute top-4 right-4 text-limestone-400 hover:text-white"
            >
              <X className="w-5 h-5" />
            </button>

            <h2 id="detail-title" className="text-lg font-semibold text-white">
              Account Metadata
            </h2>

            <div className="grid grid-cols-2 gap-3 text-xs">
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">User ID</span>
                <span className="font-mono text-white text-[11px] break-all">{detailUser.id}</span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">Email</span>
                <span className="text-white font-medium">{detailUser.email}</span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">Display Name</span>
                <span className="text-white font-medium">{detailUser.display_name}</span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">Role / Status</span>
                <span className="text-white font-medium">
                  {detailUser.role.toUpperCase()} · {detailUser.status.toUpperCase()}
                </span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">Language / Timezone</span>
                <span className="text-white font-medium">
                  {detailUser.preferred_language} · {detailUser.timezone}
                </span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">MFA Status</span>
                <span className="text-white font-medium">
                  {detailUser.mfa_enabled ? "Enabled" : "Not configured"}
                </span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">Verified At</span>
                <span className="text-white font-medium">
                  {new Date(detailUser.email_verified_at).toLocaleString()}
                </span>
              </div>
              <div className="surface p-3 bg-ink-900 border-ink-800">
                <span className="text-limestone-500 block">Created At</span>
                <span className="text-white font-medium">
                  {new Date(detailUser.created_at).toLocaleString()}
                </span>
              </div>
            </div>

            <div className="pt-2 text-right">
              <button
                type="button"
                onClick={() => setDetailUser(null)}
                className="button-secondary text-xs"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AdminUsersPage() {
  return <AdminUsersContent />;
}
