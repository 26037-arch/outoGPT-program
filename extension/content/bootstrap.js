(async () => {
  try {
    const { ChatAgent } = await import(chrome.runtime.getURL("content/chat-agent.js"));
    const agent = new ChatAgent({ debug: true });
    await agent.start();
  } catch (error) {
    console.error("[outoGPT] ChatAgent bootstrap failed", error);
  }
})();

