import type {
  Agent,
  AgentCreate,
  AgentUpdate,
  BasePrompt,
  PromptRevision,
  ModelCatalog,
  PromptPreviewRequest,
  PromptPreviewResponse,
} from "@/types/agent";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:7860/api";

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = API_BASE_URL) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }
  getBaseUrl(): string {
    return this.baseUrl;
  }
  isLocalClient(): boolean {
    return true;
  }

  private async request<T>(endpoint: string, options?: RequestInit): Promise<T> {
    if (process.env.NEXT_PUBLIC_APP_MODE === "saas") {
      throw new Error("Local management ApiClient cannot be called in SaaS mode");
    }
    const url = `${this.baseUrl}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;
    const headers = {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    };

    const response = await fetch(url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const errorBody = await response.text();
      throw new Error(`API Error [${response.status}]: ${errorBody || response.statusText}`);
    }

    return await response.json();
  }

  // Agents API
  async getAgents(): Promise<Agent[]> {
    return this.request<Agent[]>("/agents");
  }

  async getActiveAgent(): Promise<Agent | null> {
    const agents = await this.getAgents();
    return agents.find((a) => a.is_active) || null;
  }

  async getAgent(id: string): Promise<Agent> {
    return this.request<Agent>(`/agents/${id}`);
  }

  async createAgent(data: AgentCreate): Promise<Agent> {
    return this.request<Agent>("/agents", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async updateAgent(id: string, data: AgentUpdate): Promise<Agent> {
    return this.request<Agent>(`/agents/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  }

  async deleteAgent(id: string): Promise<{ status: "deleted"; id: string }> {
    return this.request<{ status: "deleted"; id: string }>(`/agents/${id}`, {
      method: "DELETE",
    });
  }

  async activateAgent(id: string): Promise<Agent> {
    return this.request<Agent>(`/agents/${id}/activate`, {
      method: "POST",
    });
  }

  async cloneAgent(id: string, newName?: string): Promise<Agent> {
    const original = await this.getAgent(id);
    const slugSuffix = Math.random().toString(36).substring(2, 8);
    const copySuffix = " (Copy)";
    const requestedName = newName?.trim() ? newName : `${original.name.slice(0, 128 - copySuffix.length)}${copySuffix}`;
    const clonedData: AgentCreate = {
      name: requestedName.slice(0, 128),
      slug: `${original.slug.slice(0, 57)}-${slugSuffix}`,
      description: original.description,
      persona_prompt: original.persona_prompt,
      llm_model: original.llm_model,
      llm_temperature: original.llm_temperature,
      llm_reasoning_effort: original.llm_reasoning_effort,
      llm_max_tokens: original.llm_max_tokens,
      stt_provider: original.stt_provider,
      tts_voice_en: original.tts_voice_en,
      tts_voice_hi: original.tts_voice_hi,
      tts_speed: original.tts_speed,
      is_active: false,
    };
    return this.createAgent(clonedData);
  }

  // Base Prompt API
  async getBasePrompt(): Promise<BasePrompt> {
    return this.request<BasePrompt>("/prompts/base");
  }

  async updateBasePrompt(content: string, change_summary?: string): Promise<BasePrompt> {
    return this.request<BasePrompt>("/prompts/base", {
      method: "PUT",
      body: JSON.stringify({
        prompt_text: content,
        content,
        change_reason: change_summary || "",
        change_summary: change_summary || "",
      }),
    });
  }

  async getPromptRevisions(): Promise<PromptRevision[]> {
    const base = await this.getBasePrompt();
    return base.revisions || [];
  }


  async previewPrompt(req: PromptPreviewRequest): Promise<PromptPreviewResponse> {
    return this.request<PromptPreviewResponse>("/prompts/preview", {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  // Catalog API
  async getModelCatalog(): Promise<ModelCatalog> {
    return this.request<ModelCatalog>("/catalog");
  }
}

export const api = new ApiClient();
