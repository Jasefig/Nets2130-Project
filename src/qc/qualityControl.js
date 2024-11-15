class ImageDataService {
  constructor() {
    // images to display to user
    this.staticImages = [
      { id: "image1", url: "../../data/image1.jpg", votes: 0 }, // need a way to aggregate total votes
      { id: "image2", url: "../../data/image2.jpg", votes: 0 },
      { id: "image3", url: "../../data/image3.jpg", votes: 0 },
      { id: "image4", url: "../../data/image4.jpg", votes: 0 },
      { id: "image5", url: "../../data/image5.jpg", votes: 0 },
    ];
  }

  // think about querying db for images
  async getImages() {
    return this.staticImages;
  }

  // changes the vote value (ie. upvote, downvote, no vote) for a given image
  async saveVote(imageId, voteValue) {
    const votes = JSON.parse(localStorage.getItem("imageVotes") || "{}");

    if (votes[imageId] === voteValue) {
      delete votes[imageId];
    } else {
      votes[imageId] = voteValue;
    }

    localStorage.setItem("imageVotes", JSON.stringify(votes));
    return votes[imageId] || 0;
  }

  async getVotes() {
    return JSON.parse(localStorage.getItem("imageVotes") || "{}");
  }
}

class ImageVotingSystem {
  constructor() {
    this.dataService = new ImageDataService();
    this.images = [];
    this.container = document.getElementById("imageContainer");
    this.init();
  }

  async init() {
    try {
      this.images = await this.dataService.getImages();
      const votes = await this.dataService.getVotes();

      this.images = this.images.map((img) => ({
        ...img,
        voteStatus: votes[img.id] || 0, // 1 for upvote, -1 for downvote
      }));

      this.render();
    } catch (error) {
      console.error("Failed to initialize:", error);
      this.showError("Failed to load images. Please try again later.");
    }
  }

  // update image status after vote
  async vote(imageId, value) {
    try {
      const newVoteStatus = await this.dataService.saveVote(imageId, value);
      const image = this.images.find((img) => img.id === imageId);
      if (image) {
        image.voteStatus = newVoteStatus;
        this.updateVote();
      }
    } catch (error) {
      console.error("Failed to save vote:", error);
      this.showError("Failed to save vote. Please try again later.");
    }
  }

  updateVote() {
    this.images.forEach((image) => {
      const upvoteButton = document.querySelector(
        `button[data-image="${image.id}"][data-value="1"]`
      );
      const downvoteButton = document.querySelector(
        `button[data-image="${image.id}"][data-value="-1"]`
      );

      if (upvoteButton && downvoteButton) {
        upvoteButton.classList.remove("active");
        downvoteButton.classList.remove("active");

        upvoteButton.classList.toggle("active", image.voteStatus === 1);
        downvoteButton.classList.toggle("active", image.voteStatus === -1);
      }
    });
  }

  createImageCard(image) {
    const card = document.createElement("div");
    card.className = "image-card";

    card.innerHTML = `
            <div class="image-wrapper">
                <img src="${image.url}" alt="Voting option ${image.id}" loading="lazy">
            </div>
            <div class="vote-buttons">
                <button class="vote-button upvote" data-image="${image.id}" data-value="1">👍</button>
                <button class="vote-button downvote" data-image="${image.id}" data-value="-1">👎</button>
            </div>
        `;

    const buttons = card.querySelectorAll(".vote-button");
    buttons.forEach((button) => {
      button.addEventListener("click", async (e) => {
        const imageId = e.target.dataset.image;
        const value = parseInt(e.target.dataset.value);
        await this.vote(imageId, value);
      });
    });

    return card;
  }

  showError(message) {
    const errorDiv = document.createElement("div");
    errorDiv.className = "error-message";
    errorDiv.textContent = message;
    this.container.prepend(errorDiv);
    setTimeout(() => errorDiv.remove(), 5000); // remove error after 5 seconds
  }

  render() {
    this.container.innerHTML = ""; // reset container
    this.images.forEach((image) => {
      const card = this.createImageCard(image);
      this.container.appendChild(card);
    });
    this.updateVote();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  new ImageVotingSystem();
});
