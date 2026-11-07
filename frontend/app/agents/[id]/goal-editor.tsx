"use client";

import { useEffect, useState, useTransition } from "react";
import { api, apiErrorText } from "@/lib/api";
import type { AgentField, AgentOutcome, FieldCondition } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";

const OPERATORS = ["eq", "ne", "in", "gt", "gte", "lt", "lte"] as const;

export function GoalEditor({
  agentId,
  agentName,
}: {
  agentId: string;
  agentName?: string;
}) {
  const [fields, setFields] = useState<AgentField[]>([]);
  const [outcomes, setOutcomes] = useState<AgentOutcome[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  // Load fields and outcomes
  useEffect(() => {
    let active = true;
    async function loadData() {
      try {
        setLoading(true);
        setError(null);
        const [loadedFields, loadedOutcomes] = await Promise.all([
          api<AgentField[]>(`/agents/${agentId}/fields`),
          api<AgentOutcome[]>(`/agents/${agentId}/outcomes`),
        ]);
        if (active) {
          setFields(loadedFields);
          setOutcomes(loadedOutcomes);
        }
      } catch (err) {
        if (active) {
          setError(apiErrorText(err, "Failed to load fields and outcomes"));
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    loadData();
    return () => {
      active = false;
    };
  }, [agentId]);

  // Field mutations
  const handleAddField = async () => {
    const fieldKey = `field_${fields.length + 1}`;
    try {
      const created = await api<AgentField>(`/agents/${agentId}/fields`, {
        method: "POST",
        body: JSON.stringify({
          field_key: fieldKey,
          description: `Collect ${fieldKey}`,
          guidance: "",
          options: [],
          required: fields.length === 0,
          required_when: [],
        }),
      });
      setFields((prev) => [...prev, created]);
    } catch (err) {
      setError(apiErrorText(err, "Failed to add field"));
    }
  };

  const handleUpdateField = async (
    fieldId: string,
    patch: Partial<AgentField>
  ) => {
    try {
      const updated = await api<AgentField>(
        `/agents/${agentId}/fields/${fieldId}`,
        {
          method: "PATCH",
          body: JSON.stringify(patch),
        }
      );
      setFields((prev) => prev.map((f) => (f.id === fieldId ? updated : f)));
    } catch (err) {
      setError(apiErrorText(err, "Failed to update field"));
    }
  };

  const handleDeleteField = async (fieldId: string) => {
    try {
      await api(`/agents/${agentId}/fields/${fieldId}`, { method: "DELETE" });
      setFields((prev) => prev.filter((f) => f.id !== fieldId));
    } catch (err) {
      setError(apiErrorText(err, "Failed to delete field"));
    }
  };

  // Outcome mutations
  const handleAddOutcome = async () => {
    const label = `outcome_${outcomes.length + 1}`;
    try {
      const created = await api<AgentOutcome>(`/agents/${agentId}/outcomes`, {
        method: "POST",
        body: JSON.stringify({
          label,
          conditions: [],
          required_fields: [],
        }),
      });
      setOutcomes((prev) => [...prev, created]);
    } catch (err) {
      setError(apiErrorText(err, "Failed to add outcome"));
    }
  };

  const handleUpdateOutcome = async (
    outcomeId: string,
    patch: Partial<AgentOutcome>
  ) => {
    try {
      const updated = await api<AgentOutcome>(
        `/agents/${agentId}/outcomes/${outcomeId}`,
        {
          method: "PATCH",
          body: JSON.stringify(patch),
        }
      );
      setOutcomes((prev) =>
        prev.map((o) => (o.id === outcomeId ? updated : o))
      );
    } catch (err) {
      setError(apiErrorText(err, "Failed to update outcome"));
    }
  };

  const handleDeleteOutcome = async (outcomeId: string) => {
    try {
      await api(`/agents/${agentId}/outcomes/${outcomeId}`, {
        method: "DELETE",
      });
      setOutcomes((prev) => prev.filter((o) => o.id !== outcomeId));
    } catch (err) {
      setError(apiErrorText(err, "Failed to delete outcome"));
    }
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Loading goal-based workflow…
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {error && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-3.5 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* Fields Section */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Information Fields</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              Define the data to collect. Universal fields are always collected;
              conditional fields activate when their conditions are satisfied.
            </p>
          </div>
          <Button size="sm" onClick={handleAddField}>
            Add field
          </Button>
        </CardHeader>
        <CardContent>
          {fields.length === 0 ? (
            <div className="rounded-xl border border-dashed py-8 text-center text-sm text-muted-foreground">
              No fields configured yet. Add one to define what the agent collects.
            </div>
          ) : (
            <div className="space-y-4">
              {fields.map((field) => (
                <div
                  key={field.id}
                  className="rounded-xl border border-border/80 bg-card p-4 space-y-3"
                >
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-4 items-center">
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Field Key
                      </Label>
                      <Input
                        value={field.field_key}
                        onChange={(e) =>
                          setFields((prev) =>
                            prev.map((f) =>
                              f.id === field.id
                                ? { ...f, field_key: e.target.value }
                                : f
                            )
                          )
                        }
                        onBlur={() =>
                          handleUpdateField(field.id, {
                            field_key: field.field_key,
                          })
                        }
                        className="mt-1 font-mono text-xs"
                      />
                    </div>
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Description / Prompt
                      </Label>
                      <Input
                        value={field.description}
                        onChange={(e) =>
                          setFields((prev) =>
                            prev.map((f) =>
                              f.id === field.id
                                ? { ...f, description: e.target.value }
                                : f
                            )
                          )
                        }
                        onBlur={() =>
                          handleUpdateField(field.id, {
                            description: field.description,
                          })
                        }
                        className="mt-1 text-xs"
                      />
                    </div>
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Options (comma-separated)
                      </Label>
                      <Input
                        value={field.options.join(", ")}
                        placeholder="e.g. order, cancel, faq"
                        onChange={(e) => {
                          const opts = e.target.value
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean);
                          setFields((prev) =>
                            prev.map((f) =>
                              f.id === field.id ? { ...f, options: opts } : f
                            )
                          );
                        }}
                        onBlur={() =>
                          handleUpdateField(field.id, {
                            options: field.options,
                          })
                        }
                        className="mt-1 text-xs"
                      />
                    </div>
                    <div className="flex items-center justify-between pt-4 sm:pt-0">
                      <label className="flex items-center gap-2 text-xs font-medium cursor-pointer">
                        <input
                          type="checkbox"
                          checked={field.required}
                          onChange={(e) =>
                            handleUpdateField(field.id, {
                              required: e.target.checked,
                            })
                          }
                          className="rounded border-border"
                        />
                        Always required
                      </label>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDeleteField(field.id)}
                        className="text-xs text-destructive hover:bg-destructive/10"
                      >
                        Delete
                      </Button>
                    </div>
                  </div>

                  {/* Conditions display */}
                  {!field.required && (
                    <div className="border-t border-border/50 pt-2 text-xs">
                      <span className="text-muted-foreground font-medium">
                        Activate when:
                      </span>
                      {field.required_when.length === 0 ? (
                        <span className="ml-2 text-muted-foreground italic">
                          Never active (add condition below)
                        </span>
                      ) : (
                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                          {field.required_when.map((cond, idx) => (
                            <Badge
                              key={idx}
                              variant="muted"
                              className="font-mono text-[11px]"
                            >
                              {cond.field_key} {cond.operator}{" "}
                              {JSON.stringify(cond.operand)}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Outcomes Section */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Completion Outcomes</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              Checkpoints that trigger when their conditions and required fields
              are filled. They confirm completion, reset conditional fields, and
              prompt for follow-up requests.
            </p>
          </div>
          <Button size="sm" onClick={handleAddOutcome}>
            Add outcome
          </Button>
        </CardHeader>
        <CardContent>
          {outcomes.length === 0 ? (
            <div className="rounded-xl border border-dashed py-8 text-center text-sm text-muted-foreground">
              No outcomes configured yet. Add one to define completion goals.
            </div>
          ) : (
            <div className="space-y-4">
              {outcomes.map((outcome) => (
                <div
                  key={outcome.id}
                  className="rounded-xl border border-border/80 bg-card p-4 space-y-3"
                >
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 items-center">
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Outcome Label
                      </Label>
                      <Input
                        value={outcome.label}
                        onChange={(e) =>
                          setOutcomes((prev) =>
                            prev.map((o) =>
                              o.id === outcome.id
                                ? { ...o, label: e.target.value }
                                : o
                            )
                          )
                        }
                        onBlur={() =>
                          handleUpdateOutcome(outcome.id, {
                            label: outcome.label,
                          })
                        }
                        className="mt-1 font-mono text-xs"
                      />
                    </div>
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Required Fields (comma-separated)
                      </Label>
                      <Input
                        value={outcome.required_fields.join(", ")}
                        placeholder="e.g. caller_name, order_items"
                        onChange={(e) => {
                          const reqs = e.target.value
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean);
                          setOutcomes((prev) =>
                            prev.map((o) =>
                              o.id === outcome.id
                                ? { ...o, required_fields: reqs }
                                : o
                            )
                          );
                        }}
                        onBlur={() =>
                          handleUpdateOutcome(outcome.id, {
                            required_fields: outcome.required_fields,
                          })
                        }
                        className="mt-1 text-xs"
                      />
                    </div>
                    <div className="flex justify-end pt-4 sm:pt-0">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDeleteOutcome(outcome.id)}
                        className="text-xs text-destructive hover:bg-destructive/10"
                      >
                        Delete
                      </Button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
