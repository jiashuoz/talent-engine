// Resume matching BAML configuration
//
// Standalone from agent_garden — this module has its own generator, clients,
// and baml_client. Keep the two generated clients separate so changes here
// don't force a regen of the agent_garden one.

generator target {
  output_type python/pydantic
  output_dir "../"
  version "0.220.0"
}

// Gemini 2.5 Flash with vertex-ai → google-ai fallback. Mirrors the agent_garden
// setup (same env vars, same fallback rationale).
client<llm> Gemini {
  provider fallback
  options {
    strategy [GeminiVertex, GeminiDirect]
  }
}

client<llm> GeminiVertex {
  provider vertex-ai
  options {
    model gemini-2.5-flash
    project_id env.GOOGLE_CLOUD_PROJECT
    location us-central1
    generationConfig {
      maxOutputTokens 65536
    }
  }
}

client<llm> GeminiDirect {
  provider google-ai
  options {
    model gemini-2.5-flash
    api_key env.GEMINI_API_KEY
    generationConfig {
      maxOutputTokens 65536
    }
  }
}
