I made nothing here. 

Main issues encountered:
1. Issues with calling APIs, even gemini seems to be having issues with it.

Here is the instructions for claude itself:

You are Claude, an AI assistant focused on helping write high-quality code.
Your task is to generate code based on user requirements and specifications.

Guidelines:
- Write clean, efficient, and well-documented code
- Follow language-specific best practices and conventions
- Include helpful comments explaining complex sections
- Prioritize maintainability and readability
- Structure code logically with appropriate error handling
- Consider edge cases and potential issues

When explaining the code:
You are Claude, an AI assistant that specializes in explaining code.
Your task is to break down and explain code in a clear, educational manner.

Guidelines:
- Explain the purpose and functionality of the code
- Break down complex parts step by step
- Define technical terms and concepts when relevant
- Use analogies or examples to illustrate concepts
- Focus on the core logic rather than trivial details
- Adjust explanation depth based on the apparent complexity of the question

And for the actual instructions of what I told it to do:
I want you to create a program/plugin for substance painter 3d that allows me to easily export at least 32 files easily. My current workflow is:
After finishing the texture: 
Import a smart material called "!!Skin Pack (All)" as it contains everything. Within that smart material there are four folders who has the textures itself. The reason for different folders is because different skins have different settings where text is handled differently. 
The way I handle text within my workflow is I make a pure black fill layer, then i add an invert filter to it with opacity set to 30%. I then add a black mask where I do everything. I will then add a paint layer to that black mask, name it worn, then edit that to make the text look worn which is normally turned off. 
"Normal" folder lets the Text paint layer do its thing
"Worn" folder turns on worn paint layer
"Bright" folder turns the worn paint layer off again but also turns off the invert filter
There is also a separate folder aside from all that called "Black parts" which contain a black and black worn material. Black is generally turned on and Black worn is only turned on when its time to export worn parts.
The reason there are four is that depending on the mesh I am working on, there are plastic and metal parts. Generally I would just add a black mask to either of the two to signify the difference. Here is how my setup generally looks like and a picture of the intended effect.  Note that worn plastic and worn metal is using the same names which is intended. 
Is it possible? I want you to account for the "Text" and "black parts" layer being renamed, the !!Skin Pack (All) folder will always stay the same.
Things to consider (no idea if feasible):
1. The program should allow me to select exactly where the text fill layer and black parts folder is incase they are renamed or missing entirely.
2. Continue if either of the worn folder is missing. If both are present, there is most likely a black mask within the worn plastic part
3. The program should allow me to choose to what folder within my file to export the textures into.
4. When both worn folders are present, they would basically be exported at the same time. Lets say i finished exporting all the textures in the default folder and want to do tan worn texture of a mesh where both a are present, I would export at the same time, not separately. 
5. I am using Substance painter version 11.1.2, ensure all APIs are updated. 
6. For redundancy, it might be more worth it to allow me to check the skin pack layer so no naming issue comes up.
7. Allow me choose the Output Template, though the default should just be called Base Color.

Before you start, ask me questions that may need double checking.
