const { Client, GatewayIntentBits } = require('discord.js');
const { joinVoiceChannel, VoiceConnectionStatus, EndBehaviorType } = require('@discordjs/voice');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
require('dotenv').config();

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildVoiceStates,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent
    ]
});

const TOKEN = process.env.DISCORD_BOT_TOKEN;
const CHANNEL_ID = process.env.DISCORD_VOICE_CHANNEL_ID;
const PYTHON_URL = process.env.PYTHON_API_URL || 'http://localhost:5000/api/voice_message';

client.once('ready', async () => {
    console.log(`[NodeJS Bridge] Logged in successfully as ${client.user.tag}`);

    try {
        let channel = client.channels.cache.get(CHANNEL_ID);
        if (!channel) {
            channel = await client.channels.fetch(CHANNEL_ID);
        }

        if (channel && channel.isVoiceBased()) {
            const connection = joinVoiceChannel({
                channelId: channel.id,
                guildId: channel.guild.id,
                adapterCreator: channel.guild.voiceAdapterCreator,
                selfDeaf: false,
                selfMute: true
            });

            connection.on(VoiceConnectionStatus.Ready, () => {
                console.log('[NodeJS Bridge] Connected to voice channel and listening for speech!');
                
                const receiver = connection.receiver;

                receiver.speaking.on('start', (userId) => {
                    listenToUser(receiver, userId, channel.guild);
                });
            });
        } else {
            console.error('[NodeJS Bridge] Error: Provided CHANNEL_ID is not a valid voice channel.');
        }
    } catch (error) {
        console.error('[NodeJS Bridge] Failed to connect to voice:', error);
    }
});

async function listenToUser(receiver, userId, guild) {
    if (userId === client.user.id) return;

    try {
        const member = await guild.members.fetch(userId);
        if (member.user.bot) return;

        console.log(`[NodeJS Bridge] Detected speech from ${member.displayName}...`);

        const audioStream = receiver.subscribe(userId, {
            end: {
                behavior: EndBehaviorType.Silence,
                duration: 1000,
            },
        });

        const outputPath = path.join(__dirname, `recording_${userId}.pcm`);
        const writeStream = fs.createWriteStream(outputPath);
        audioStream.pipe(writeStream);

        writeStream.on('finish', async () => {
            console.log(`[NodeJS Bridge] Finished recording audio packet from ${member.displayName}`);
            
            // Uncomment this once your Python endpoint / Whisper transcription hook is active:
            /*
            try {
                await axios.post(PYTHON_URL, {
                    user: member.displayName,
                    text: "Hello Lumi from voice channel" 
                });
            } catch (err) {
                console.error("[NodeJS Bridge] Failed to forward message to Python:", err.message);
            }
            */
        });

    } catch (error) {
        console.error('[NodeJS Bridge error handling user speech]:', error);
    }
}

client.login(TOKEN);