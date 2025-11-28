# app/core/benchmarks.py

PREBUILT_BENCHMARKS = [
    {
        "name": "Cross-Lingual Similarity (EN/FR/ES)",
        "groups": [
            {
                "id": "g1", "name": "Greetings", "color": "#3b82f6",
                "texts": ["Hello, how are you today?", "Bonjour, comment ça va aujourd'hui ?", "Hola, ¿cómo estás hoy?"]
            },
            {
                "id": "g2", "name": "Food", "color": "#f59e0b",
                "texts": ["I would like to order a pizza.", "Je voudrais commander une pizza.", "Me gustaría pedir una pizza."]
            },
            {
                "id": "g3", "name": "Technology", "color": "#10b981",
                "texts": ["Artificial intelligence is a fascinating field.", "L'intelligence artificielle est un domaine fascinant.", "La inteligencia artificial es un campo fascinante."]
            }
        ]
    },
    {
        "name": "Semantic Nuance (English)",
        "groups": [
            {
                "id": "g1", "name": "Positive Emotion", "color": "#22c55e",
                "texts": ["He was overjoyed with the result.", "She felt ecstatic after the concert.", "They were thrilled to see their friends."]
            },
            {
                "id": "g2", "name": "Negative Emotion", "color": "#ef4444",
                "texts": ["The news left him feeling devastated.", "She was miserable during the storm.", "He expressed deep sorrow for his actions."]
            },
            {
                "id": "g3", "name": "Walking", "color": "#0ea5e9",
                "texts": ["He sauntered down the street.", "She strolled through the park.", "They ambled along the beach."]
            }
        ]
    },
    {
        "name": "Topic Separation (Technical vs. Nature)",
        "groups": [
            {
                "id": "g1", "name": "Programming", "color": "#8b5cf6",
                "texts": ["Python is a versatile programming language.", "Debugging is a crucial part of software development.", "An API allows two applications to talk to each other."]
            },
            {
                "id": "g2", "name": "Nature", "color": "#16a34a",
                "texts": ["The forest is home to many species of birds.", "Photosynthesis is the process plants use to convert light into energy.", "A river flows from its source to the sea."]
            }
        ]
    }
]
