from pydantic import BaseModel
from typing import Dict, Any, Optional


class ModelConfig(BaseModel):
    """Configuration for a single LLM model."""
    name: str
    provider: str
    description: str = ""
    parameters: Dict[str, Any]  # Model-specific parameters (flexible)


class PromptsConfig(BaseModel):
    """Prompt templates for different scenarios."""
    state_based: str
    chat_based: str
    action_based: Optional[str] = None
    tool_result_based: Optional[str] = None
    # Персонажи кампании: ключ → имя, род занятий, манера речи, обращение к игроку.
    campaign_personas: Optional[Dict[str, Dict[str, str]]] = None


class LLMConfig(BaseModel):
    """Main LLM configuration."""
    model_type: str  # 'qwen' or 'llama'
    models: Dict[str, ModelConfig]
    prompts: Optional[PromptsConfig] = None

    def get_active_model(self) -> ModelConfig:
        """Get the currently active model configuration."""
        if self.model_type not in self.models:
            raise ValueError(f"Unknown model type: {self.model_type}")
        return self.models[self.model_type]

    def get_model_name(self) -> str:
        """Get the name of the active model."""
        return self.get_active_model().name

    def get_parameter(self, param_name: str, default=None) -> Any:
        """Get a specific parameter from the active model."""
        model = self.get_active_model()
        return model.parameters.get(param_name, default)

    def get_parameters(self) -> Dict[str, Any]:
        """Get all parameters for the active model."""
        return self.get_active_model().parameters.copy()

    def has_parameter(self, param_name: str) -> bool:
        """Check if a parameter exists in the active model."""
        model = self.get_active_model()
        return param_name in model.parameters

    def get_state_prompt_template(self) -> str:
        """Get the state-based prompt template."""
        if self.prompts is None:
            raise ValueError("LLM prompts are not loaded")
        return self.prompts.state_based

    def get_chat_prompt_template(self) -> str:
        """Get the chat-based prompt template."""
        if self.prompts is None:
            raise ValueError("LLM prompts are not loaded")
        return self.prompts.chat_based

    def get_action_prompt_template(self) -> Optional[str]:
        """Get the structured action/chat prompt template."""
        if self.prompts is None:
            raise ValueError("LLM prompts are not loaded")
        return self.prompts.action_based

    def get_tool_result_prompt_template(self) -> Optional[str]:
        """Get the prompt template used after tool execution."""
        if self.prompts is None:
            raise ValueError("LLM prompts are not loaded")
        return self.prompts.tool_result_based

