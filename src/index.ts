// Sample TypeScript file to activate Serena MCP server

export class HelloWorld {
  private message: string;

  constructor(message: string = "Hello, World!") {
    this.message = message;
  }

  public greet(): string {
    return this.message;
  }

  public setMessage(newMessage: string): void {
    this.message = newMessage;
  }
}

// Example usage
const hello = new HelloWorld();
console.log(hello.greet());
