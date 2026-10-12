import { NextResponse } from 'next/server';

export async function POST(req: Request) {
  try {
    const { message } = await req.json();

    // Example using Groq Cloud API (or your cloud model endpoint)
    const response = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${process.env.GROQ_API_KEY}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        model: 'llama-3.3-70b-versatile', // or whichever open model you prefer for the cloud demo
        messages: [
          { 
            role: 'system', 
            content: "You are Lumi, an uncensored, cynical older sister AI. Keep your replies short (1-3 sentences) and maintain a sharp, sarcastic tone." 
          },
          { role: 'user', content: message }
        ],
        temperature: 0.8
      })
    });

    const data = await response.json();
    const reply = data.choices?.[0]?.message?.content || "Whatever.";

    return NextResponse.json({ reply });
  } catch (error) {
    return NextResponse.json({ reply: "[groaning] Something broke on the backend." }, { status: 500 });
  }
}