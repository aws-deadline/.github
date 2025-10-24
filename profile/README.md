# AWS Deadline Cloud GitHub Org

Home for open source code related to [AWS Deadline Cloud](https://aws.amazon.com/deadline-cloud/). AWS Deadline Cloud is a fully managed render farm service that simplifies infrastructure management and scaling for teams delivering computer generated imagery.

## Integrations
This GitHub org has integrations (such as job submission plugins) for a selection of digital content creation (DCC) software. These integrations allow users to submit jobs from inside their favorite applications. See the [integrations user guide](https://aws-deadline.github.io/) for more info about features, usage, and compatibility.

 | Software | Repository
 | - | -
 | [Adobe After Effects](https://www.adobe.com/products/aftereffects.html) | [deadline-cloud-for-after-effects](https://github.com/aws-deadline/deadline-cloud-for-after-effects)
 | [Autodesk 3ds Max](https://www.autodesk.com/products/3ds-max/overview) | [deadline-cloud-for-3ds-max](https://github.com/aws-deadline/deadline-cloud-for-3ds-max)
 | [Autodesk Arnold for Maya](https://www.autodesk.com/products/arnold/overview) | [deadline-cloud-for-maya](https://github.com/aws-deadline/deadline-cloud-for-maya)
 | [Autodesk Maya](https://www.autodesk.com/products/maya/overview/) | [deadline-cloud-for-maya](https://github.com/aws-deadline/deadline-cloud-for-maya)
 | [Autodesk VRED](https://www.autodesk.com/products/vred/overview/) | [deadline-cloud-for-vred](https://github.com/aws-deadline/deadline-cloud-for-vred)
 | [Blender](https://blender.org) | [deadline-cloud-for-blender](https://github.com/aws-deadline/deadline-cloud-for-blender)
 | [Chaos V-Ray for Maya](https://www.chaos.com/vray/maya) | [deadline-cloud-for-maya](https://github.com/aws-deadline/deadline-cloud-for-maya)
 | [Foundry Nuke](https://www.foundry.com/products/nuke-family/nuke) | [deadline-cloud-for-nuke](https://github.com/aws-deadline/deadline-cloud-for-nuke)
 | [KeyShot Studio](https://www.keyshot.com/) | [deadline-cloud-for-keyshot](https://github.com/aws-deadline/deadline-cloud-for-keyshot)
 | [Maxon Cinema 4D](https://www.maxon.net/en/cinema-4d) | [deadline-cloud-for-cinema-4d](https://github.com/aws-deadline/deadline-cloud-for-cinema-4d)
 | [SideFX Houdini](https://www.sidefx.com/) | [deadline-cloud-for-houdini](https://github.com/aws-deadline/deadline-cloud-for-houdini)
 | [Unreal Engine](https://www.unrealengine.com) | [deadline-cloud-for-unreal-engine](https://github.com/aws-deadline/deadline-cloud-for-unreal-engine)
 
## Service clients and sample repositories

| Repository | Description |
| - | - |
| [deadline-cloud](https://github.com/aws-deadline/deadline-cloud) | A Python CLI and library for interacting with Deadline Cloud, especially submitting jobs. |
| [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples) | Example code and tools for running workloads on Deadline Cloud. |
| [deadline-cloud-worker-agent](https://github.com/aws-deadline/deadline-cloud-worker-agent) | Software that runs on workers and communicates with the Deadline Cloud API for receiving and completing tasks. |

## FAQ

- **How do I install Deadline Cloud tools on my workstation?**

    You can run the Deadline Cloud submitter installer. The installer can be found by going to the AWS Console > AWS Deadline Cloud > Downloads > Deadline Cloud submitter installer > Download.

- **Where can I find documentation for the AWS Deadline Cloud service?**

    See the [AWS Deadline Cloud documentation](https://docs.aws.amazon.com/deadline-cloud/).

- **How do I report a bug with an integration or tool?**

    Please open an issue in the relevant repository.

-  **How do I build my own integration?**

    You can look through [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples) for example job bundles, conda recipes, and other tooling. You can also look through the pre-built integrations below for larger examples.

- **Where can I find information about defining jobs?**


    Deadline uses Open Job Description for defining jobs. See the [Open Job Description specification](https://github.com/OpenJobDescription/openjd-specifications/) or other repositories in the [Open Job Description GitHub org](https://github.com/OpenJobDescription/).

- **Do you accept feature requests or contributions?**

    Yes, contributions are gratefully accepted! You can open an issue to request a feature or receive early feedback on an idea before raising a pull request. Pull requests are welcomed for features that would be widely useful to the community and which align with the existing code base.

- **What if I want to adapt an existing integration to my specific workload?**

    Consider forking a repository and making your changes in the fork.
