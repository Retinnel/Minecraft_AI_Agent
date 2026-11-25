package com.example.assistant;

public class AssistantConfig {
    public String serverUrl = "http://127.0.0.1:8000";
    public int sendIntervalTicks = 20;
    public int adviceIntervalTicks = 40;
    public boolean showAdviceInActionbar = true;
    // «Сделай сам»: ниже этого здоровья автопилот встаёт на паузу.
    public int autopilotMinHealth = 6;
}
