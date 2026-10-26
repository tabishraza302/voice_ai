export interface AgentLoadFailure {
  notFound: boolean;
  detail: string;
}

/** Shown when the API answered 404, so the agent really is gone. */
const NOT_FOUND_DETAIL = "This agent no longer exists. It may have been deleted.";

/** Shown when there is no message to report, e.g. an empty error or a blank value. */
const NO_RESPONSE_DETAIL = "The API did not return a response.";

/** The prefix `ApiClient.request` puts on every non-2xx response. */
const NOT_FOUND_MARKER = "API Error [404]";

/**
 * Turns anything a `catch` block can see into copy the page can show.
 *
 * A 404 is the only failure that means "this agent is gone"; every other
 * failure keeps the underlying message so the page never claims an agent was
 * deleted when the API merely failed. This function never throws.
 */
export function describeAgentLoadError(error: unknown): AgentLoadFailure {
  const message = readMessage(error);

  if (message.includes(NOT_FOUND_MARKER)) {
    return { notFound: true, detail: NOT_FOUND_DETAIL };
  }

  return {
    notFound: false,
    detail: message.length > 0 ? message : NO_RESPONSE_DETAIL,
  };
}

/** `error.message` for an `Error`, `String(error)` otherwise, trimmed. */
function readMessage(error: unknown): string {
  try {
    return (error instanceof Error ? error.message : String(error)).trim();
  } catch {
    // A value whose toString/`message` getter throws is still not a reason to
    // crash the page: report it as "no response" instead.
    return "";
  }
}
