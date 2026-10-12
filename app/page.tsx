'use client';
import { useState } from 'react';

export default function LumiPlayground() {
  const [messages, setMessages] = useState([
    { role: 'assistant', content: "Well, what do you want? Keep it quick." }
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);

  const sendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userMessage = input;
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setLoading(true);

    try {
      // Connect to your backend route or a serverless function handling the LLM call
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userMessage })
      });
      const data = await res.json();
      setMessages(prev => [...prev, { role: 'assistant', content: data.reply }]);
    } catch (err) {
      setMessages(prev => [...prev, { role: 'assistant', content: "[sighing] Great, the network broke. Try again." }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="flex flex-col h-screen max-w-xl mx-auto p-4 bg-gray-900 text-gray-100">
      <header className="py-4 border-b border-gray-800 mb-4">
        <h1 className="text-xl font-bold">Lumi Playground (Limited Preview)</h1>
        <p className="text-sm text-gray-400">Uncensored older sister AI companion demo.</p>
      </header>

      <div className="flex-1 overflow-y-auto space-y-4 pr-2">
        {messages.map((m, i) => (
          <div key={i} className={`p-3 rounded-lg ${m.role === 'user' ? 'bg-indigo-600 ml-auto max-w-[80%]' : 'bg-gray-800 mr-auto max-w-[80%]'}`}>
            <p className="text-sm">{m.content}</p>
          </div>
        ))}
        {loading && <div className="text-gray-500 text-sm animate-pulse">Lumi is typing...</div>}
      </div>

      <form onSubmit={sendMessage} className="mt-4 flex gap-2">
        <input 
          type="text" 
          value={input} 
          onChange={e => setInput(e.target.value)}
          placeholder="Say something to Lumi..."
          className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-indigo-500"
        />
        <button type="submit" className="bg-indigo-600 px-4 py-2 rounded text-sm font-medium hover:bg-indigo-500">Send</button>
      </form>
    </main>
  );
}